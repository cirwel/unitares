"""First-cut dV/dt analysis on real UNITARES EISV trajectories (pulled 2026-06-17)."""
import numpy as np, matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from datetime import datetime

# --- Real data: agent f92dcea8 recent_history (10 pts, ~5 min cadence) ---
ts = ["08:42:36","08:47:33","08:52:33","08:57:33","09:02:32",
      "09:07:28","09:12:26","09:17:30","09:22:26","09:27:24"]
t = np.array([ (datetime.strptime(x,"%H:%M:%S")-datetime.strptime(ts[0],"%H:%M:%S")).total_seconds()/60 for x in ts])
V = np.array([0.10023351,0.10057184,0.10075495,0.10081276,0.10077198,
              0.10065583,0.10048366,0.10027107,0.10002961,0.09976737])
S = np.array([0.21478645,0.22111891,0.23116809,0.23970979,0.24697020,
              0.25314155,0.25838720,0.26284603,0.26196939,0.26589093])
E = np.array([0.78578582,0.78467661,0.78370469,0.78285216,0.78210333,
              0.78144462,0.78086428,0.78035209,0.77989713,0.77948968])
I = np.array([0.68080759,0.68105985,0.68130170,0.68151915,0.68169830,
              0.68183414,0.68193020,0.68199433,0.68204060,0.68208248])

dV = np.gradient(V, t); dS = np.gradient(S, t); dEmI = np.gradient(E-I, t)

print("=== f92dcea8: derivative cut (stable window, no pause) ===")
print(f"V range        : {V.min():.5f}..{V.max():.5f}  (Δ={np.ptp(V):.5f})")
print(f"S range        : {S.min():.5f}..{S.max():.5f}  (Δ={np.ptp(S):.5f})  monotonic-up={np.all(np.diff(S)>0)}")
print(f"dV/dt sign flips: {np.sum(np.diff(np.sign(dV))!=0)}  (V humps then resolves)")
print(f"V peak at t=+{t[np.argmax(V)]:.0f}min, dV/dt crosses 0 there; S still rising")
# corr of level vs derivative against a 'stress' proxy = S (chromaticism)
print(f"corr(V_level, S)   = {np.corrcoef(V,S)[0,1]:+.3f}")
print(f"corr(dV/dt,  dS/dt)= {np.corrcoef(dV,dS)[0,1]:+.3f}")
print(f"corr(dV/dt,  E-I)  = {np.corrcoef(dV,(E-I))[0,1]:+.3f}  (dV tracks the imbalance, by construction)")

fig, ax = plt.subplots(2,1, figsize=(8,6), sharex=True)
ax[0].plot(t, V, "o-", color="#b5179e", label="V (E−I imbalance / harmonic tension)")
ax[0].plot(t, S, "s-", color="#4361ee", label="S (semantic uncertainty / chromaticism)")
ax[0].set_ylabel("level"); ax[0].legend(loc="center left"); ax[0].set_title("f92dcea8 — real EISV, stable window (level)")
ax[1].axhline(0, color="#888", lw=.8)
ax[1].plot(t, dV, "o-", color="#b5179e", label="dV/dt  (tension rate)")
ax[1].plot(t, dS, "s-", color="#4361ee", label="dS/dt  (chromatic rate)")
ax[1].fill_between(t, dV, 0, where=dV>0, color="#b5179e", alpha=.15)
ax[1].set_ylabel("derivative / min"); ax[1].set_xlabel("minutes from window start"); ax[1].legend(loc="upper right")
ax[1].set_title("the affect lives here: dV/dt humps positive then resolves while S keeps climbing")
plt.tight_layout(); plt.savefig("first_cut.png", dpi=130)
print("\nfigure -> .scratch/tonality/first_cut.png")
