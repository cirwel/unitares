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

**Synthetic canonical pause episode** (ILLUSTRATIVE — hand-built, not measured;
the read API doesn't expose deep pre-pause trajectories, see the first-cut note):

- `synthetic_pause_episode.wav` (+ `synthetic_pause_score.png`) — settled-in-key →
  tension builds (V↑, I↓, S↑, coherence↓) → loss of key → pause. You hear the
  melody go chromatic and flat while the harmony leans hard dominant and never
  cadences. This is what the failure boundary *sounds* like.

## Honesty notes

- Real renders use genuine logged EISV but only the shallow *recent* window the
  read API exposes; none contain an actual pause. The pause episode is synthetic
  and labeled as such.
- `S` here drives chromaticism as a heuristic-blend magnitude, not a measured
  entropy (consistent with `docs/EISV_COMPUTATION.md`).
- v0 mappings (register/intonation/tension curves) are choices, not claims; the
  paper's human-detection study is what would validate that these renderings make
  drift *faster to detect* than the dashboard.

## Next

- Wire to a live trajectory stream (`get_eisv_trajectory_state` already emits
  `{dE,dI,dS,dV}` + shape labels) for real-time monitoring audio.
- Run the detection study: can listeners flag an impending pause from audio
  earlier than analysts reading EISV plots?
