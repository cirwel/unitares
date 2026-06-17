"""
sonify v0.2 — one morphing field, no delivery artifacts.

Kenny's critique of v0/v0.1: a *sequence of bars* smuggles in tempo (the chunk
rate) and melody (the bar grid) as audible structure that encodes the RENDERING
METHOD, not the agent — "a graph accidentally presenting information about graphs."
v0.2 removes them: the whole episode is ONE sustained chord on a FIXED root that
*morphs continuously*. Every audible change corresponds to an EISV change; nothing
is introduced by delivery. Time is mapped from the REAL check-in cadence, so the
rate of souring is the real rate of drift (not an imposed beat).

Mapping (encoding choices, not artifacts):
  S (entropy)   -> dissonance: low S = pure major triad; rising S adds M2, b7,
                   tritone, and detuned-root beating. (the foreground signal)
  I (integrity) -> intonation: low I detunes the WHOLE field flat (cents). Calibration = tuning.
  E (energy)    -> brightness: high E = rich overtones; low E = dull. ("tired" = dull, not low-pitched)
  risk          -> sub-bass swell + amplitude tremor, ONLY above a threshold
                   (so a healthy field is NOT ominous — kills the horror baseline).
  V (imbalance) -> DEMOTED: a near-inaudible few-cents global bias (the slow integral / memory).
  coherence     -> omitted (not persisted), neutral.

A healthy EISV (high E/I, low S/risk) therefore renders as a bright, in-tune,
restful C-major field — the consonant "home" — and drift sours away from it.

Usage:
  python3 sonify_v02.py        # renders: home (healthy ref), the pre-pause episode, the settled agent
"""
import json, math, struct, wave
import sonify  # for the REAL agent fingerprints

SR = 44100
ROOT_HZ = 261.63          # C4, FIXED — no pitch-melody
S_HOME, S_MAX = 0.13, 0.38
E_LO, E_HI = 0.30, 0.82
I_HOME, I_LO = 0.85, 0.55
RISK_GATE = 0.30

# Chord members as (semitone offset, base amplitude, dissonance-gated?)
SKELETON = [(0, 0.50, False), (4, 0.34, False), (7, 0.40, False)]   # root, M3, P5 (major triad)
TENSION = [(2, 0.30), (10, 0.30), (6, 0.26)]                        # M2, b7, tritone (gated by S)


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def drivers(E, I, S, V, risk):
    diss = clamp((S - S_HOME) / (S_MAX - S_HOME))
    bright = clamp((E - E_LO) / (E_HI - E_LO))
    detune_cents = -clamp((I_HOME - I) / (I_HOME - I_LO)) * 40.0   # flat when I low
    detune_cents += V * 5.0                                        # V: tiny demoted bias
    risk_swell = clamp((risk - RISK_GATE) / (1.0 - RISK_GATE))
    return diss, bright, detune_cents, risk_swell


def interp(states, ts, t):
    """Linear EISV interpolation at real-time t (ts in seconds from start)."""
    if t <= ts[0]:
        return states[0]
    if t >= ts[-1]:
        return states[-1]
    for i in range(1, len(ts)):
        if t <= ts[i]:
            f = (t - ts[i - 1]) / (ts[i] - ts[i - 1] or 1e-9)
            a, b = states[i - 1], states[i]
            return {k: a[k] + f * (b[k] - a[k]) for k in ("E", "I", "S", "V", "risk")}
    return states[-1]


def render_field(states, ts, out_seconds):
    """Block-based additive synthesis of one continuously morphing chord."""
    n = int(out_seconds * SR)
    BLK = 441                                   # ~10ms control rate
    span = ts[-1] - ts[0] or 1.0

    # oscillator bank: (semitone, kind) ; kinds: skel, tension(idx), beat, subbass, overtone(parent,k)
    oscs = []
    for st, _, _ in SKELETON:
        oscs.append(("skel", st, 1))
        oscs.append(("over", st, 2)); oscs.append(("over", st, 3))   # overtones for brightness
    for j, (st, _) in enumerate(TENSION):
        oscs.append(("tens", st, j))
    oscs.append(("beat", 0, 0))                 # detuned root -> beating with S
    oscs.append(("sub", 0, 0))                  # sub-bass with risk
    phase = [0.0] * len(oscs)
    amp_prev = [0.0] * len(oscs)
    buf = [0.0] * n

    blk_start = 0
    trem_phase = 0.0
    while blk_start < n:
        blk = min(BLK, n - blk_start)
        t_real = ts[0] + span * (blk_start / n)
        e = interp(states, ts, t_real)
        diss, bright, dcents, risk_sw = drivers(e["E"], e["I"], e["S"], e["V"], e["risk"])
        detune = 2 ** (dcents / 1200.0)
        # amplitude tremor depth from risk (instability you can hear), slow ~5Hz
        trem_rate = 5.0

        # target freq/amp per oscillator
        freqs, amps = [], []
        for kind, st, k in oscs:
            base = ROOT_HZ * (2 ** (st / 12.0)) * detune
            if kind == "skel":
                f, a = base, dict(SKELETON_AMP).get(st, 0.3)
            elif kind == "over":
                f, a = base * k, dict(SKELETON_AMP).get(st, 0.3) * (0.35 * bright) / k
            elif kind == "tens":
                f, a = base, TENSION[k][1] * (diss if k < 2 else diss * diss)
            elif kind == "beat":
                f, a = ROOT_HZ * detune * (1 + 0.012 * diss), 0.28 * diss
            else:  # sub
                f, a = ROOT_HZ / 4.0 * detune, 0.30 * risk_sw
            freqs.append(f); amps.append(a)

        for oi in range(len(oscs)):
            f = freqs[oi]; a0 = amp_prev[oi]; a1 = amps[oi]
            w = 2 * math.pi * f / SR
            ph = phase[oi]
            for i in range(blk):
                a = a0 + (a1 - a0) * (i / blk)
                s = a * math.sin(ph)
                # apply risk tremor to sub + beat for instability
                if oscs[oi][0] in ("sub", "beat") and risk_sw > 0:
                    s *= 1.0 - 0.5 * risk_sw * (0.5 + 0.5 * math.sin(trem_phase + 2 * math.pi * trem_rate * i / SR))
                buf[blk_start + i] += s
                ph += w
            phase[oi] = ph % (2 * math.pi)
            amp_prev[oi] = a1
        trem_phase = (trem_phase + 2 * math.pi * trem_rate * blk / SR) % (2 * math.pi)
        blk_start += blk
    return buf


SKELETON_AMP = [(st, a) for st, a, _ in SKELETON]


def write_wav(buf, path):
    peak = max(1e-9, max(abs(x) for x in buf))
    pcm = b"".join(struct.pack("<h", int(32767 * 0.89 * x / peak)) for x in buf)
    with wave.open(path, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(pcm)
    print(f"  wrote {path}  ({len(buf)/SR:.1f}s)")


def states_times_from_rows(rows):
    from datetime import datetime
    t0 = datetime.fromisoformat(rows[0]["t"])
    ts = [(datetime.fromisoformat(r["t"]) - t0).total_seconds() for r in rows]
    states = [{k: r[k] for k in ("E", "I", "S", "V", "risk")} for r in rows]
    return states, ts


if __name__ == "__main__":
    OUT = 22.0
    # 1) HOME: a steady healthy EISV — the consonant reference. Should sound bright/restful/in-tune.
    home = [dict(E=0.85, I=0.90, S=0.11, V=0.0, risk=0.05)] * 2
    print("HOME reference (healthy, steady):")
    write_wav(render_field(home, [0.0, 60.0], 8.0), "v02_home_healthy.wav")

    # 2) The real pre-pause episode (real cadence) — should sour toward the pause.
    d = json.load(open("pre_pause_windows.json"))
    ep = next(e for e in d["episodes"] if e["tier"] == "TIER_A" and e["pause_at"][:16] == "2026-06-13 15:48")
    st, ts = states_times_from_rows(ep["rows"])
    print(f"Pre-pause episode id{ep['identity_id']} {ep['pause_at'][:16]} (real cadence):")
    write_wav(render_field(st, ts, OUT), f"v02_episode_id{ep['identity_id']}_prepause.wav")

    # 3) The 'tired' settled agent — should be dull + in-tune + calm, NOT ominous.
    h = sonify.REAL["69a1a4f7_deep-settled_V-0.41"]
    rows = [{"E": h["E"][i], "I": h["I"][i], "S": h["S"][i], "V": h["V"][i], "risk": h["risk"][i]}
            for i in range(len(h["E"]))]
    st3 = [{k: r[k] for k in ("E", "I", "S", "V", "risk")} for r in rows]
    ts3 = [i * 300.0 for i in range(len(rows))]   # no real timestamps for this one -> even 5-min spacing (noted)
    print("Settled 'tired' agent 69a1a4f7 (even spacing — no real timestamps available):")
    write_wav(render_field(st3, ts3, 10.0), "v02_settled_69a1a4f7.wav")
