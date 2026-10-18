#!/usr/bin/env python3
"""UNITARES terminal dashboard: a live, read-only view over the `unitares` CLI.

Usage: unitares tui [--interval SECONDS] [--once [--json]] [--no-color]

Data comes from `unitares status --json` and `unitares tools`, so URL, token
and header handling stay in one place (scripts/unitares). Standard library only.

Keys: up/down or j/k select - Enter drill in - Esc/b back - t tools - r refresh - q quit
"""
import argparse
import curses
import json
import os
import subprocess
import sys
import time

CLI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "unitares")
FETCH_TIMEOUT = 20
VERDICT_COLOR = {"proceed": 1, "approve": 1, "guide": 2, "pause": 3, "reject": 3}
STATUS_COLOR = {"healthy": 1, "silent": 3}
KEYS = "up/down select | Enter detail | Esc back | t tools | r refresh | q quit"


def run_cli(*args):
    """Run the sibling CLI; return (stdout, error)."""
    try:
        proc = subprocess.run(
            [CLI, *args], capture_output=True, text=True, timeout=FETCH_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return "", (proc.stderr.strip() or f"exit {proc.returncode}").splitlines()[0]
    return proc.stdout, ""


def fetch_status():
    out, err = run_cli("status", "--json", "--no-release")
    if err:
        return None, err
    try:
        return json.loads(out), ""
    except ValueError as exc:
        return None, f"bad JSON from status: {exc}"


def fetch_tools():
    out, err = run_cli("tools")
    return out.splitlines(), err


def fmt_age(seconds):
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds // 86400}d"


def bar(value, width=20):
    """Render a 0..1 value as a bar; clamp anything outside."""
    value = max(0.0, min(1.0, value))
    filled = int(round(value * width))
    return "[" + "#" * filled + "." * (width - filled) + f"] {value:.2f}"


def overview_lines(data):
    """Return [(text, color_key)] for the overview screen."""
    health = data.get("health", {})
    db = health.get("database", {}).get("status", "?")
    up = health.get("uptime", {}).get("formatted", "?")
    tiers = data.get("tiers", {})
    out = [
        (
            f"UNITARES {health.get('status', '?')} - v{health.get('version', '?')}"
            f" ({health.get('build_sha', '?')}) - up {up} - db {db}",
            1 if health.get("status") == "ok" else 3,
        )
    ]
    counts = tiers.get("tiers", {})
    if counts:
        parts = " | ".join(f"{k} {v}" for k, v in counts.items())
        out.append((f"Agents: {tiers.get('total', '?')} total - earned {tiers.get('earned', '?')}", 0))
        out.append((f"Tiers:  {parts}", 0))
    else:
        out.append(("Agents: tier data unavailable (auth-gated or older server)", 2))
    out.append(("", 0))
    return out


def resident_rows(data):
    residents = data.get("residents", {}).get("residents", [])
    return [
        (
            r.get("label", "?"),
            r.get("status", "?"),
            r.get("verdict", "-"),
            fmt_age(r.get("silence_seconds")),
        )
        for r in residents
    ]


def resident_detail_lines(resident):
    eisv = resident.get("eisv") or {}
    lines = [
        (f"{resident.get('label', '?')}  status {resident.get('status', '?')}  "
         f"verdict {resident.get('verdict', '-')}", STATUS_COLOR.get(resident.get("status"), 0)),
        (f"agent_id   {resident.get('agent_id', '?')}", 0),
        (f"last check-in {fmt_age(resident.get('silence_seconds'))} ago via "
         f"{resident.get('last_checkin_source', '?')}"
         f"{' (event-driven)' if resident.get('event_driven') else ''}", 0),
        (f"silence threshold {fmt_age(resident.get('silence_threshold_seconds'))}", 0),
        ("", 0),
    ]
    for key, name in (("E", "energy"), ("I", "integrity"), ("S", "entropy")):
        if key in eisv:
            lines.append((f"{key} {name:<9} {bar(eisv[key])}", 0))
    if "V" in eisv:
        lines.append((f"V valence   {eisv['V']:+.3f}  (EMA of E - I, range -1..1)", 0))
    if resident.get("coherence") is not None:
        lines.append((f"coherence   {bar(resident['coherence'])}", 0))
    if resident.get("risk_score") is not None:
        lines.append((f"risk        {bar(resident['risk_score'])}", 0))
    history = resident.get("history") or []
    if history:
        verdicts = [h.get("verdict", "?") for h in history[-30:]]
        lines.append(("", 0))
        lines.append((f"last {len(verdicts)} check-ins: " + " ".join(v[0].upper() for v in verdicts), 0))
    writes = resident.get("recent_writes") or []
    if writes:
        lines.append(("", 0))
        lines.append(("Recent findings:", 0))
        for w in writes[:5]:
            lines.append((f"  [{w.get('severity', '?')}] {w.get('summary', '')}", 2))
    return lines


class App:
    def __init__(self, interval):
        self.interval = interval
        self.data, self.error = None, ""
        self.tools, self.tools_error = None, ""
        self.view = "overview"
        self.selected = 0
        self.scroll = 0
        self.last_fetch = 0.0

    def refresh(self):
        self.data, self.error = fetch_status()
        self.last_fetch = time.time()
        if self.view == "tools":
            self.tools, self.tools_error = fetch_tools()

    def residents(self):
        if not self.data:
            return []
        return self.data.get("residents", {}).get("residents", [])

    def handle(self, key):
        """Return False to quit."""
        n = len(self.residents())
        if key in (ord("q"), ord("Q")):
            return False
        if key in (ord("r"), ord("R")):
            self.refresh()
        elif key in (curses.KEY_DOWN, ord("j")):
            if self.view == "tools":
                self.scroll += 1
            elif n:
                self.selected = (self.selected + 1) % n
        elif key in (curses.KEY_UP, ord("k")):
            if self.view == "tools":
                self.scroll = max(0, self.scroll - 1)
            elif n:
                self.selected = (self.selected - 1) % n
        elif key in (curses.KEY_ENTER, 10, 13) and self.view == "overview" and n:
            self.view = "detail"
        elif key in (27, ord("b"), ord("o")):
            self.view, self.scroll = "overview", 0
        elif key in (ord("t"), ord("T")):
            self.view, self.scroll = "tools", 0
            self.tools, self.tools_error = fetch_tools()
        return True

    def draw(self, scr):
        scr.erase()
        height, width = scr.getmaxyx()
        row = 0

        def put(text, color=0, bold=False):
            nonlocal row
            if row >= height - 1:
                return
            attr = curses.color_pair(color) if color else 0
            if bold:
                attr |= curses.A_BOLD
            try:
                scr.addnstr(row, 0, text, max(0, width - 1), attr)
            except curses.error:
                pass
            row += 1

        if self.data is None:
            put(f"UNITARES unreachable: {self.error or 'no data yet'}", 3, True)
            put("Check `unitares diag`; press r to retry, q to quit.", 2)
        elif self.view == "overview":
            for text, color in overview_lines(self.data):
                put(text, color)
            put(f"{'RESIDENT':<20}{'STATUS':<10}{'VERDICT':<10}LAST SIGNAL", 0, True)
            for i, (label, status, verdict, age) in enumerate(resident_rows(self.data)):
                marker = ">" if i == self.selected else " "
                line = f"{marker} {label:<18}{status:<10}{verdict:<10}{age}"
                color = STATUS_COLOR.get(status) or VERDICT_COLOR.get(verdict, 0)
                if i == self.selected:
                    try:
                        scr.addnstr(row, 0, line.ljust(width - 1), width - 1,
                                    curses.A_REVERSE | (curses.color_pair(color) if color else 0))
                    except curses.error:
                        pass
                    row += 1
                else:
                    put(line, color)
        elif self.view == "detail":
            residents = self.residents()
            if residents:
                for text, color in resident_detail_lines(residents[self.selected % len(residents)]):
                    put(text, color)
        elif self.view == "tools":
            if self.tools_error:
                put(f"Could not list tools: {self.tools_error}", 3)
            else:
                lines = self.tools or []
                self.scroll = min(self.scroll, max(0, len(lines) - (height - 2)))
                for text in lines[self.scroll:]:
                    put(text)
        age = int(time.time() - self.last_fetch) if self.last_fetch else 0
        footer = f" {KEYS} | refreshed {age}s ago "
        try:
            scr.addnstr(height - 1, 0, footer.ljust(width - 1), width - 1, curses.A_REVERSE)
        except curses.error:
            pass
        scr.refresh()


def main_curses(scr, interval, use_color=True):
    curses.curs_set(0)
    if use_color and curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_RED, -1)
    app = App(interval)
    app.refresh()
    scr.timeout(500)
    while True:
        app.draw(scr)
        key = scr.getch()
        if key != -1 and not app.handle(key):
            return
        if time.time() - app.last_fetch >= interval:
            app.refresh()


def snapshot(data):
    """Stable, minimal machine-readable view of `status --json` (schema v1)."""
    health = data.get("health", {})
    tiers = data.get("tiers", {})
    return {
        "schema": "unitares.tui.snapshot.v1",
        "health": {
            "status": health.get("status"),
            "version": health.get("version"),
            "build_sha": health.get("build_sha"),
            "uptime_seconds": health.get("uptime", {}).get("seconds"),
            "database": health.get("database", {}).get("status"),
        },
        "tiers": {
            "total": tiers.get("total"),
            "earned": tiers.get("earned"),
            "counts": tiers.get("tiers"),
        },
        "residents": [
            {
                "label": r.get("label"),
                "agent_id": r.get("agent_id"),
                "status": r.get("status"),
                "verdict": r.get("verdict"),
                "silence_seconds": r.get("silence_seconds"),
                "coherence": r.get("coherence"),
                "risk_score": r.get("risk_score"),
                "eisv": r.get("eisv"),
            }
            for r in data.get("residents", {}).get("residents", [])
        ],
    }


def print_once(as_json=False):
    data, err = fetch_status()
    if data is None:
        if as_json:
            print(json.dumps({"schema": "unitares.tui.snapshot.v1", "error": err}))
        else:
            print(f"UNITARES unreachable: {err}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(snapshot(data), indent=2))
        return 0
    for text, _ in overview_lines(data):
        print(text)
    print(f"{'RESIDENT':<20}{'STATUS':<10}{'VERDICT':<10}LAST SIGNAL")
    for label, status, verdict, age in resident_rows(data):
        print(f"  {label:<18}{status:<10}{verdict:<10}{age}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="unitares tui", description=__doc__.split("\n")[0])
    parser.add_argument("--interval", type=float, default=10.0, help="refresh seconds (default 10)")
    parser.add_argument("--once", action="store_true", help="print one snapshot and exit (no TTY needed)")
    parser.add_argument("--json", action="store_true", help="with --once: print a stable JSON snapshot")
    parser.add_argument("--no-color", action="store_true", help="no color (also honored: NO_COLOR env)")
    args = parser.parse_args(argv)
    if args.json and not (args.once or not sys.stdout.isatty()):
        parser.error("--json needs --once (or a non-TTY stdout)")
    if args.once or not sys.stdout.isatty():
        return print_once(as_json=args.json)
    use_color = not (args.no_color or os.environ.get("NO_COLOR"))
    curses.wrapper(main_curses, max(2.0, args.interval), use_color)
    return 0


if __name__ == "__main__":
    sys.exit(main())
