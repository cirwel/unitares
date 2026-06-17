"""
sonify v0.3 — corrupt the consonance (make drift audible), still artifact-free.

v0.2 was honest but imperceptible: "major chords with different brightness, like
vibrato/organ." Three fixes, all still strict monotonic functions of EISV (higher
gain on a real signal, not invented drama):

  1. Intonation as BEATING, not global detune. Low I detunes chord members
     *against each other* (root/fifth get beating twins) -> audible roughness.
     A uniform flat shift is inaudible; interval beating is unambiguously "out of tune."
  2. Dissonance CORRUPTS the triad instead of garnishing it. Rising S bends the
     third flat (major -> neutral/"wrong" third), brings in a b9 semitone clash
     against the root, and weakens the fifth's anchor -> the harmony loses its
     major identity and destabilizes toward atonal.
  3. Steeper gain so the episode's real S/I range produces an unmistakable swing.

Still: one sustained chord, fixed root, continuous morph at the REAL check-in
cadence; no beat, no melody. Healthy EISV -> clean bright in-tune C major (home);
drift -> the chord goes rough, the third goes wrong, a sub-bass dread arrives with risk.

Usage:
  python3 sonify_v03.py
"""
import json, math
from sonify_v02 import clamp, write_wav, states_times_from_rows, interp
import sonify

SR = 44100
ROOT_HZ = 261.63
S_HOME, S_MAX = 0.13, 0.36
E_LO, E_HI = 0.30, 0.82
I_HOME, I_LO = 0.85, 0.58
RISK_GATE = 0.30


def drivers(E, I, S, V, risk):
    diss = clamp((S - S_HOME) / (S_MAX - S_HOME))
    rough = clamp((I_HOME - I) / (I_HOME - I_LO))      # interval beating (calibration error)
    bright = clamp((E - E_LO) / (E_HI - E_LO))
    risk_sw = clamp((risk - RISK_GATE) / (1.0 - RISK_GATE))
    return diss, bright, rough, risk_sw


def render_field(states, ts, out_seconds):
    n = int(out_seconds * SR)
    BLK = 441
    span = ts[-1] - ts[0] or 1.0
    # fixed oscillator slots (role) -> phase continuity; freq/amp recomputed per block
    slots = ["root", "root2", "root3", "fifth", "fifth2", "third",
             "b9", "b7", "tritone", "rootbeat", "fifthbeat", "sub"]
    phase = [0.0] * len(slots)
    amp_prev = [0.0] * len(slots)
    buf = [0.0] * n
    trem_phase = 0.0
    i0 = 0
    while i0 < n:
        blk = min(BLK, n - i0)
        e = interp(states, ts, ts[0] + span * (i0 / n))
        diss, bright, rough, risk_sw = drivers(e["E"], e["I"], e["S"], e["V"], e["risk"])
        third_semi = 4.0 - 2.6 * diss                  # major(4) -> neutral/wrong(~1.4)
        spread = 16.0 + 55.0 * rough + 30.0 * diss     # cents; beating widens w/ low-I & high-S
        bf = 2 ** (spread / 1200.0)
        fifth_hz = ROOT_HZ * 2 ** (7 / 12.0)

        freqs = {
            "root": ROOT_HZ, "root2": ROOT_HZ * 2, "root3": ROOT_HZ * 3,
            "fifth": fifth_hz, "fifth2": fifth_hz * 2,
            "third": ROOT_HZ * 2 ** (third_semi / 12.0),
            "b9": ROOT_HZ * 2 ** (1 / 12.0), "b7": ROOT_HZ * 2 ** (10 / 12.0),
            "tritone": ROOT_HZ * 2 ** (6 / 12.0),
            "rootbeat": ROOT_HZ * bf, "fifthbeat": fifth_hz * bf,
            "sub": ROOT_HZ / 4.0,
        }
        beat_gate = clamp(0.35 * rough + 0.5 * diss)
        amps = {
            "root": 0.50, "root2": 0.30 * bright, "root3": 0.18 * bright,
            "fifth": 0.40 * (1 - 0.5 * diss), "fifth2": 0.20 * bright,
            "third": 0.34,
            "b9": 0.34 * diss ** 1.2,                  # semitone clash
            "b7": 0.22 * diss,
            "tritone": 0.30 * diss ** 1.5,
            "rootbeat": 0.40 * beat_gate, "fifthbeat": 0.30 * beat_gate,
            "sub": 0.36 * risk_sw,
        }
        for si, role in enumerate(slots):
            f, a0, a1 = freqs[role], amp_prev[si], amps[role]
            w = 2 * math.pi * f / SR
            ph = phase[si]
            for i in range(blk):
                a = a0 + (a1 - a0) * (i / blk)
                s = a * math.sin(ph)
                if role == "sub" and risk_sw > 0:      # risk -> unstable sub
                    s *= 1 - 0.6 * risk_sw * (0.5 + 0.5 * math.sin(trem_phase + 2 * math.pi * 5 * i / SR))
                buf[i0 + i] += s
                ph += w
            phase[si] = ph % (2 * math.pi)
            amp_prev[si] = a1
        trem_phase = (trem_phase + 2 * math.pi * 5 * blk / SR) % (2 * math.pi)
        i0 += blk
    return buf


if __name__ == "__main__":
    print("HOME reference (healthy, steady):")
    write_wav(render_field([dict(E=.85, I=.90, S=.11, V=0, risk=.05)] * 2, [0, 60], 8.0),
              "v03_home_healthy.wav")

    d = json.load(open("pre_pause_windows.json"))
    ep = next(e for e in d["episodes"] if e["tier"] == "TIER_A" and e["pause_at"][:16] == "2026-06-13 15:48")
    st, ts = states_times_from_rows(ep["rows"])
    print(f"Pre-pause episode id{ep['identity_id']} (real cadence):")
    write_wav(render_field(st, ts, 22.0), f"v03_episode_id{ep['identity_id']}_prepause.wav")

    h = sonify.REAL["69a1a4f7_deep-settled_V-0.41"]
    st3 = [{"E": h["E"][i], "I": h["I"][i], "S": h["S"][i], "V": h["V"][i], "risk": h["risk"][i]}
           for i in range(len(h["E"]))]
    print("Settled 'tired' agent 69a1a4f7 (even spacing):")
    write_wav(render_field(st3, [i * 300.0 for i in range(len(st3))], 10.0), "v03_settled_69a1a4f7.wav")
