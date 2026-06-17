"""
sonify v0.1 — salience hierarchy corrected (foreground S, demote V).

v0 mapped the most prominent audible gesture (harmonic tension/lean) to V — which
the lead-time analysis showed is the doubly-smoothed integral (flat, range <0.012).
The informative, fast axis is S (entropy/chromaticism, range up to 0.11). This
renderer inverts the salience to match the information hierarchy:

  S (entropy)  -> FOREGROUND: drives harmonic dissonance/density + roughness + the
                  chromatic-alteration rate. Rising S = audibly building tension.
  V (imbalance)-> DEMOTED: a faint background pedal shift only (the system's memory).
  E, I, risk   -> unchanged (register / intonation / sub-bass).
  coherence    -> omitted (not persisted; neutral), as in v0.

It reuses v0's synthesis primitives by overriding sonify.render_state, so the WAV
writer / normalization stay identical and the A/B is apples-to-apples.

Usage:
  python3 sonify_v01.py            # render the A/B pair on the best real episode
"""
import json
import sonify
from sonify import TONIC, midi_to_hz, tone, noise, mix, adsr, SR, DIATONIC


def render_state_S_foreground(E, I, S, V, coherence, risk, dur, rng):
    """One EISV state -> one bar, with S as the dominant audible mover."""
    n = int(dur * SR)
    bar = [0.0] * n
    # S drives tension now (was V). Absolute mapping so episodes stay comparable.
    tension = max(0.0, min(1.0, (S - 0.20) / 0.25))
    bright = 1.0  # coherence not persisted -> neutral (no inharmonic smear from coherence)

    # --- bass chord: density/dissonance scales with S ---
    root = TONIC - 24
    chord = [(root, 0.5), (root + 7, 0.35), (root + 4, 0.30)]
    chord.append((root + 10, 0.30 * tension))           # b7 — adds bite as S rises
    chord.append((root + 2, 0.24 * tension))            # maj 2nd — density
    if tension > 0.5:
        chord.append((root + 6, 0.26 * tension * tension))  # tritone shimmer at high S
    # V demoted: a faint pedal a hair off the root, amplitude tiny and ~constant.
    chord.append((root + (1 if V > 0 else -1) * 0, 0.06))   # background ground, ~inaudible mover
    for m, a in chord:
        mix(bar, tone(midi_to_hz(m), dur, a * 0.5, partials=(1, .5, .25), env=False), 0)

    # --- melody: register from E, intonation from I, chromaticism rate from S ---
    sub = dur / 2
    for k in range(2):
        deg = round(E * 7) + (0 if k == 0 else (1 if E < 0.5 else -1))
        deg = max(0, min(13, deg))
        m = TONIC + DIATONIC[deg % 7] + 12 * (deg // 7)
        if rng.random() < S * 1.6:                      # S -> more chromatic alteration (foregrounded)
            m += rng.choice([-1, 1])
        cents = -(1 - I) * 45.0
        amp = 0.30 + 0.16 * E
        mix(bar, tone(midi_to_hz(m, cents), sub * 0.95, amp, partials=(1, .4, .2)),
            int(k * sub * SR))
        # roughness: a detuned double whose beating strength tracks S (the audible "unease")
        if tension > 0.05:
            mix(bar, tone(midi_to_hz(m, cents + 12 * tension), sub * 0.95, amp * 0.5 * tension,
                          partials=(1, .3)), int(k * sub * SR))

    # --- risk -> sub-bass unease (unchanged) ---
    if risk > 0.25:
        mix(bar, tone(midi_to_hz(TONIC - 36), dur, 0.18 * (risk - 0.25) * 2,
                      partials=(1,), env=False), 0)
    # high S also adds a thin noise bed (uncertainty you can hear)
    if tension > 0.4:
        mix(bar, noise(dur, 0.012 * tension), 0)
    return bar


def render(states, path):
    """Render with the S-foreground mapping, reusing v0's writer/normalization."""
    saved = sonify.render_state
    sonify.render_state = render_state_S_foreground
    try:
        sonify.sonify(states, path)
    finally:
        sonify.render_state = saved


def best_sustained_episode():
    """The TIER_A episode where S rises AND stays high into the pause (the fair test)."""
    d = json.load(open("pre_pause_windows.json"))
    a = [e for e in d["episodes"] if e["tier"] == "TIER_A"]
    # rank by S at the pause row minus S baseline (sustained rise)
    def sustained(e):
        rows = e["rows"]
        base = sum(r["S"] for r in rows[:3]) / 3
        pause = next((r for r in rows if r["action"] in
                      {"risk_pause", "cirs_block", "void_pause", "coherence_pause"}), rows[-1])
        return pause["S"] - base
    return max(a, key=sustained)


if __name__ == "__main__":
    ep = best_sustained_episode()
    tag = f"id{ep['identity_id']}_{ep['pause_at'][:16].replace(' ', 'T').replace(':', '')}"
    print(f"A/B on the best sustained-S episode: {tag} ({ep['pause_action']})")
    sonify.sonify(ep["states"], f"ab_OLD_Vforeground_{tag}.wav")     # v0 mapping
    render(ep["states"], f"ab_NEW_Sforeground_{tag}.wav")            # v0.1 mapping
    print("  listen: OLD = lean (V) loud but flat; NEW = chromatic tension (S) builds into the pause")
