# EISV Sonification — prototype v0

Renders a UNITARES agent's EISV trajectory as audible music: governance state
*heard* as a tonal control loop. This is the method behind the
"tonal sonification as an interpretability interface" paper angle — making
`docs/tonality-metaphor.md`'s mapping not a metaphor but an instrument.

`python3 sonify.py` → four mono 16-bit WAVs (no external synth/libs; stdlib only).

## The mapping (audible)

| EISV | Musical parameter | You hear |
|---|---|---|
| **E** energy / productive capacity | melodic register | higher E → higher melody |
| **I** integrity / calibration | intonation (cents) | lower I → flatter, out of tune |
| **S** semantic uncertainty | chromaticism | higher S → more off-key (accidental) notes |
| **V** E−I imbalance / void | harmonic tension + lean | \|V\| → tension; **V>0** dominant-7 pull, **V<0** suspended-4th sit-back |
| **coherence** | resolution / brightness | low coherence → inharmonic partials + noise bed |
| **risk** | sub-bass drone | risk > 0.25 → low unease tone |

Key is fixed to **C major** across all renders, so different agents are directly
comparable — each agent's characteristic EISV becomes a *tonal fingerprint*.

## Renders

**Real agent fingerprints** (EISV pulled from the live deployment 2026-06-17,
recent stable windows — all "proceed", no pause in-window):

- `agent_f92dcea8_energy-surplus_V+0.10.wav` — V≈+0.10, high E. Leans **dominant** (forward pull). S climbs across the clip → harmony gradually colors chromatic.
- `agent_9a6681ec_integrity-surplus_V-0.40.wav` — V≈−0.40, low E / high I. Sits on the **suspended-4th / plagal** side; risk oscillates → restless melody.
- `agent_69a1a4f7_deep-settled_V-0.41.wav` — V≈−0.41, very low risk, 124k updates. Same suspended lean but **calm and in tune** — the "deeply settled, integrity-heavy" voice.

The audible contrast between the +V agent and the two −V agents is real harmonic
contrast straight from real data: energy-surplus *pulls forward*, integrity-surplus
*sits back*.

**Real pre-pause episodes** (extracted from the governance DB — supersede the
synthetic episode for *transition-into-pause* cases; see `extract_pre_pause_windows.py`):

`python3 extract_pre_pause_windows.py --render` walks `core.agent_state` for pauses
whose preceding check-in was a *proceed* (a real "the pause came" transition, not a
sustained plateau), pulls the preceding ~12 check-ins at full resolution, and renders
one WAV per episode. The v0 synthetic episode was built because the *read API* exposes
only a shallow recent window — but the full pre-pause trajectory was already **persisted**
all along (every check-in writes E/I/S/V + risk + verdict + action to `agent_state`).
So pre-pause windows needed *retrieval*, not new retention.

Example real episode (`id3701` / Sentinel, 2026-06-10, `risk_pause`): E and I drift
down, risk climbs in steps (0.15 → 0.45 `guide` → 0.51 → **0.75 `risk_pause`**) — the
audible approach to the boundary, from logged data.

**Synthetic canonical pause episode** (ILLUSTRATIVE — hand-built, retained as a
clean reference shape):

- `synthetic_pause_episode.wav` (+ `synthetic_pause_score.png`) — settled-in-key →
  tension builds (V↑, I↓, S↑, coherence↓) → loss of key → pause. You hear the
  melody go chromatic and flat while the harmony leans hard dominant and never
  cadences. This is what the failure boundary *sounds* like — idealized; the real
  episodes above are noisier and more gradual.

## Lead-time validation — does the sound actually lead the dashboard?

`python3 lead_time_analysis.py --plot` tests the core thesis ("you hear the pause
coming before the verdict flips") on the real episodes instead of asserting it. It
guards against the **circularity trap**: the pause verdict is a threshold on `risk`,
and `risk` is computed *from* EISV — so a risk-vs-risk "lead" is just the threshold
gap, not evidence the sound helps. The real test is whether an *audible, different-axis*
channel (S→chromaticism, V→harmonic lean) crosses its onset before the **verdict label**
a dashboard analyst watches (`approve → guide/pause`).

**Verdict on current fuel (n=3, all Sentinel/3701, all the #686 spurious class):**

- **V (the headline harmonic-lean channel) is FLAT — fires in 0/3, total range ≤0.012.**
  The dominant/sus4 lean the synthetic episode leaned on carries ~no leading signal here.
- **S (chromaticism) onset precedes the label in 3/3, but sustains into the pause in only
  2/3** — the third is a transient bump that *resolves* before the pause (a misleading
  lead; see `lead_time_id3701_2026-06-10_*.png`: S rises then falls as risk snaps up).
- **risk snaps** (median max step ≈0.06/min) — pauses are threshold crossings, not gradual
  approaches, so even the circular risk channel gives little lead.

So: **audible lead is NOT demonstrated, and the sample is biased** toward the worst case
(spurious z-score pauses are abrupt by construction — there is nothing real to hear coming).
The harness is built and the verdict is honest; a *fair* test needs non-spurious, gradual
pauses to accumulate (auto-`TIER_A` from 2026-06-08 on). This also redirects the mapping:
on real data S, not V, is the channel doing the work — the opposite of the synthetic premise.

## Finding: the salience hierarchy is inverted (S is the signal, V is memory)

Chasing *why* V is flat turned up a design principle, not just a data quirk.

`V` is `EMA(EMA(E) − EMA(I))` — doubly smoothed; the code comment calls it
"accumulated, not instantaneous." It is the **integral/memory term** by construction
(`src/behavioral_state.py`). Asking it to *lead* is asking the integral to predict.
Measured ranges across the 3 TIER_A episodes:

| | range V | range E−I | range S |
|---|---|---|---|
| ep1 | 0.006 | 0.036 | **0.081** |
| ep2 | 0.003 | 0.008 | **0.110** |
| ep3 | 0.012 | 0.042 | **0.075** |

V's double-smoothing hides some signal (E−I moves 2–6× more than V), but the real point
is the next column: **S moves 2–14× more than E−I.** The information lives on the
entropy/uncertainty axis, not the imbalance axis — lagged or not.

So the prototype **inverted its salience hierarchy**: it mapped the most musically
prominent channel (harmonic tension/lean) to the quietest, most-lagged, least-informative
coordinate (V), and mapped a subtler channel (chromaticism) to the loudest, fastest,
most-informative one (S). The architecture predicts this — S is the fast channel
(α=0.15, ~175s half-life); V is the doubly-smoothed integral.

Tonal theory agrees once the mapping is right: **chromaticism is the leading edge of
harmonic motion** — accidentals signal you're leaving the key *before* the harmony
confirms it (the precision-weighted-prediction-error reading already in
`docs/tonality-metaphor.md`). Design principle for v0.1: **audible salience should
mirror the information/control hierarchy** — foreground the fast informative axes
(S/chromaticism, and the S/risk *derivatives*), demote V/harmonic-lean to a slowly
shifting background ground (the system's memory, not its alarm).

## Honesty notes

- Real renders use genuine logged EISV but only the shallow *recent* window the
  read API exposes; none contain an actual pause. The pause episode is synthetic
  and labeled as such.
- `S` here drives chromaticism as a heuristic-blend magnitude, not a measured
  entropy (consistent with `docs/EISV_COMPUTATION.md`).
- v0 mappings (register/intonation/tension curves) are choices, not claims; the
  paper's human-detection study is what would validate that these renderings make
  drift *faster to detect* than the dashboard.
- **`coherence` is not persisted in modern `agent_state` rows** (only 227 legacy ODE
  rows carry it). The extractor does **not** fabricate it — real episodes omit
  coherence, so sonify falls back to its neutral 0.5; `phi` is carried in the JSON as
  a diagnostic only, never substituted for coherence.
- **The real 4-D fuel is thin and narrow.** Of ~143 persisted pauses (23 healthy→pause
  transitions), only **3 render as full 4-D EISV — all one agent (Sentinel/3701), all
  the PR #686 spurious-pause class.** `behavioral_eisv` only went 100%-always-on the
  week of 2026-06-08, so older pauses lack I/S/V (the extractor tiers them `TIER_B`,
  E+risk only). New pauses are auto-`TIER_A`: the study's fuel grows by **accumulation**,
  not by adding instrumentation. n=3/1-agent is enough to build and validate the
  pipeline, **not** enough for a publishable detection study yet.

## Next

- Wire to a live trajectory stream (`get_eisv_trajectory_state` already emits
  `{dE,dI,dS,dV}` + shape labels) for real-time monitoring audio.
- **Map derivatives, not levels.** `first_cut.py` found the affect lives in `dV/dt`
  (it humps positive then resolves while S keeps climbing), but `sonify.py` renders
  per-state *levels*. Mapping `dV/dt → forward lean / micro-accelerando` and
  `dS/dt → rate of chromatic encroachment` likely explains why the synthetic episode
  feels alive and near-static real windows don't — and it's the same `{dE,dI,dS,dV}`
  the live stream already emits.
- Run the detection study once enough diverse `TIER_A` pauses have accumulated
  (currently 3, one agent): can listeners flag an impending pause from audio earlier
  than analysts reading EISV plots?
