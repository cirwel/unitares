"""
EISV → tonal audio sonification (prototype v0).

Renders a UNITARES agent's EISV trajectory as audible music, so an agent's
governance state can be *heard* as a tonal control loop. Pure-Python additive
synthesis → 16-bit PCM WAV; no external synth or libraries beyond the stdlib.

Mapping (the load-bearing claim of docs/tonality-metaphor.md, made audible):

  E (energy / productive capacity)  → melodic register   (higher E = higher)
  I (integrity / calibration)       → intonation          (lower I = flatter, detuned)
  S (semantic uncertainty)          → chromaticism         (higher S = more off-key notes)
  V (E−I imbalance / void)          → harmonic tension     (sign chooses the lean:
                                        V>0 dominant-7 pull forward; V<0 sus/plagal sit-back)
  coherence (restoring feedback)    → resolution / brightness (low coh = inharmonic, noisy)
  risk                              → sub-bass unease drone

Key is fixed to C major across all renders so different agents are directly
comparable — each agent's characteristic EISV becomes a tonal fingerprint.
"""
import math, struct, wave, random

SR = 44100
TONIC = 60                       # C4
DIATONIC = [0, 2, 4, 5, 7, 9, 11]  # C major scale degrees


def midi_to_hz(m, cents=0.0):
    return 440.0 * 2 ** ((m - 69) / 12.0 + cents / 1200.0)


def adsr(n, a=0.02, d=0.1, s=0.7, r=0.2):
    env = []
    na, nd, nr = int(a * SR), int(d * SR), int(r * SR)
    for i in range(n):
        if i < na:
            env.append(i / max(na, 1))
        elif i < na + nd:
            env.append(1 - (1 - s) * (i - na) / max(nd, 1))
        elif i < n - nr:
            env.append(s)
        else:
            env.append(s * max(0, (n - i) / max(nr, 1)))
    return env


def tone(freq, dur, amp, partials=(1.0, 0.5, 0.25, 0.12), inharmon=0.0, env=True):
    n = int(dur * SR)
    e = adsr(n) if env else [1.0] * n
    buf = [0.0] * n
    for k, pa in enumerate(partials, start=1):
        f = freq * k * (1 + inharmon * k * 0.004)   # inharmonicity smears partials
        w = 2 * math.pi * f / SR
        for i in range(n):
            buf[i] += pa * math.sin(w * i)
    return [amp * e[i] * buf[i] for i in range(n)]


def noise(dur, amp):
    n = int(dur * SR)
    return [amp * (random.random() * 2 - 1) for _ in range(n)]


def mix(into, src, at):
    for i, v in enumerate(src):
        j = at + i
        if j < len(into):
            into[j] += v


def render_state(E, I, S, V, coherence, risk, dur, rng):
    """One EISV state → one 'bar' of audio."""
    n = int(dur * SR)
    bar = [0.0] * n
    tension = min(abs(V) / 0.45, 1.0)
    bright = max(0.0, min(1.0, coherence / 0.6))     # coherence → harmonic clarity
    inharm = (1 - bright) * 1.0                       # low coherence → inharmonic

    # --- bass harmonic-tension chord (sustained whole bar) ---
    root = TONIC - 24            # C2
    fifth = root + 7
    third = root + 4
    chord = [(root, 0.5), (fifth, 0.35), (third, 0.30 * (1 - tension))]
    if V > 0:                    # energy surplus → dominant-7 lean (pull forward)
        chord.append((root + 10, 0.32 * tension))     # Bb : minor 7th
    else:                        # integrity surplus → suspended 4th (sit back)
        chord.append((root + 5, 0.32 * tension))      # F  : the unresolved 4th
    if tension > 0.55:           # deep imbalance → tritone shimmer
        chord.append((root + 6, 0.22 * tension * tension))
    for m, a in chord:
        mix(bar, tone(midi_to_hz(m), dur, a * 0.5, partials=(1, .5, .25),
                      inharmon=inharm, env=False), 0)

    # --- melody: 2 subdivisions, register from E, off-key from S, flat from I ---
    sub = dur / 2
    for k in range(2):
        deg = round(E * 7) + (0 if k == 0 else (1 if E < 0.5 else -1))
        deg = max(0, min(13, deg))
        m = TONIC + DIATONIC[deg % 7] + 12 * (deg // 7)
        if rng.random() < S * 1.3:                    # S → chromatic alteration
            m += rng.choice([-1, 1])
        cents = -(1 - I) * 45.0                        # I → intonation (flat when low)
        amp = 0.32 + 0.18 * E
        mix(bar, tone(midi_to_hz(m, cents), sub * 0.95, amp,
                      partials=(1, .4 * bright, .2 * bright), inharmon=inharm),
            int(k * sub * SR))

    # --- risk → sub-bass unease ---
    if risk > 0.25:
        mix(bar, tone(midi_to_hz(TONIC - 36), dur, 0.18 * (risk - 0.25) * 2,
                      partials=(1,), env=False), 0)
    if bright < 0.85:                                  # low coherence → noise bed
        mix(bar, noise(dur, 0.015 * (1 - bright)), 0)
    return bar


def sonify(states, path, dur=0.85, seed=7):
    rng = random.Random(seed)
    audio = []
    for s in states:
        audio += render_state(s["E"], s["I"], s["S"], s["V"],
                              s.get("coherence", 0.5), s.get("risk", 0.2), dur, rng)
    peak = max(1e-9, max(abs(x) for x in audio))
    pcm = b"".join(struct.pack("<h", int(32767 * 0.89 * x / peak)) for x in audio)
    with wave.open(path, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(pcm)
    print(f"  wrote {path}  ({len(audio)/SR:.1f}s, {len(states)} states)")


def series(ts_dict):
    """Transpose parallel *_history arrays into a list of state dicts."""
    h = ts_dict
    n = len(h["E"])
    return [{"E": h["E"][i], "I": h["I"][i], "S": h["S"][i], "V": h["V"][i],
             "coherence": h["coh"][i], "risk": h["risk"][i]} for i in range(n)]


# ---- REAL agent trajectories pulled from the live deployment 2026-06-17 ----
REAL = {
 "f92dcea8_energy-surplus_V+0.10": {
   "E":[.78578582,.78467661,.78370469,.78285216,.78210333,.78144462,.78086428,.78035209,.77989713,.77948968],
   "I":[.68080759,.68105985,.68130170,.68151915,.68169830,.68183414,.68193020,.68199433,.68204060,.68208248],
   "S":[.21478645,.22111891,.23116809,.23970979,.24697020,.25314155,.25838720,.26284603,.26196939,.26589093],
   "V":[.10023351,.10057184,.10075495,.10081276,.10077198,.10065583,.10048366,.10027107,.10002961,.09976737],
   "coh":[.49854604,.49824907,.49789971,.49757146,.49729314,.49707038,.49689789,.49676795,.49667204,.49659811],
   "risk":[.24380891,.23311792,.23519426,.23361447,.23318997,.23298265,.23287575,.23280057,.23604640,.23379778]},
 "9a6681ec_integrity-surplus_V-0.40": {
   "E":[.38142977,.38494976,.38644770,.38905179,.39012529,.39178996,.39322608,.39520326,.39539157,.39494829],
   "I":[.79383630,.79898409,.79785828,.79169446,.79116422,.79052000,.79061892,.78962113,.78891995,.78792568],
   "S":[.18484712,.16712009,.17081275,.16527146,.16933892,.17279625,.17583249,.17851079,.18078735,.18359992],
   "V":[-.41415323,-.41414134,-.41386826,-.41274570,-.41157503,-.41029053,-.40900076,-.40754247,-.40614106,-.40482469],
   "coh":[.49122145,.49124278,.49124671,.49121733,.49125275,.49130879,.49135365,.49148643,.49161519,.49165696],
   "risk":[.32950521,.28017895,.23973219,.35881922,.29821427,.26039562,.37985347,.35139549,.28072390,.23633659]},
 "69a1a4f7_deep-settled_V-0.41": {
   "E":[.39057780,.39157647,.39294249,.39410859,.39542276,.39631523,.39697820,.39586242,.39378133,.39135717],
   "I":[.80093555,.80024471,.79916913,.79866760,.79787819,.79767994,.79756954,.79761198,.79735502,.79747062],
   "S":[.19266928,.19256888,.19308355,.19367102,.19492037,.19523231,.19744746,.19888034,.19844829,.20063105],
   "V":[-.42243948,-.42106236,-.41957879,-.41807681,-.41651467,-.41499968,-.41355884,-.41237791,-.41149749,-.41095909],
   "coh":[.49743079,.49742581,.49741929,.49741184,.49740358,.49739654,.49739030,.49738376,.49738004,.49737670],
   "risk":[.18763440,.18762509,.18755954,.18755735,.18749418,.18768188,.18748493,.18750518,.18769367,.18748557]},
}

# ---- SYNTHETIC canonical pause episode (ILLUSTRATIVE, not measured) ----
# settled-in-key → tension builds (V↑, I↓, S↑) → loss of coherence → pause.
def synthetic_pause(n_settle=5, n_build=6, n_break=5):
    states = []
    for i in range(n_settle):
        states.append(dict(E=.50, I=.72, S=.14, V=.00, coherence=.55, risk=.20))
    for i in range(n_build):
        f = (i + 1) / n_build
        states.append(dict(E=.50 + .38*f, I=.72 - .17*f, S=.14 + .41*f,
                           V=.00 + .32*f, coherence=.55 - .13*f, risk=.20 + .45*f))
    for i in range(n_break):
        f = (i + 1) / n_break
        states.append(dict(E=.88, I=.55, S=.55 + .20*f, V=.32,
                           coherence=.42 - .05*f, risk=.66 + .08*f))
    return states


if __name__ == "__main__":
    print("Rendering real agent tonal fingerprints:")
    for name, h in REAL.items():
        sonify(series(h), f"agent_{name}.wav")
    print("Rendering synthetic canonical pause episode (illustrative):")
    sonify(synthetic_pause(), "synthetic_pause_episode.wav",
           dur=0.85)
