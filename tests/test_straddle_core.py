"""Property tests for straddle_core.

Run standalone:  python tests/test_straddle_core.py
Run under pytest: pytest tests/test_straddle_core.py

These are PROPERTY tests, not golden-value tests. Each one pins a behaviour
that was wrong in the CSV-era screener and must not regress:

  1  forward_rv aggregates the h-step variance path (not sigma_1 * sqrt(h))
  2  FHS keeps fat tails; constant-sigma resampling collapses to Gaussian
  3  E|X| = 0.798*sigma is the straddle price; 1 sigma is a different number
  4  TFR is monotone in kurtosis and equals 1.0 for a Gaussian
  5  POP RISES with fat tails -- the reason it is excluded from the ranking
  6  exercise STT scales with intrinsic and dominates a deep-ITM finish
  7  a k*credit stop implies an unreachable move on an ATM straddle
  8  the exit rule changes the P&L distribution, so it must be inside the sim
  9  scoring survives NaN inputs and ranks in the declared direction
 10  universe demeaning removes the common index-vol factor
 11  gates, including the "no event data" != "no event" distinction
 12  forecast diagnostics recover known parameters
 13  path efficiency separates trend from chop
"""
import math
import sys
from pathlib import Path

import numpy as np
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import straddle_core as sc  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        fails.append(name)


rng = np.random.default_rng(7)

# ---- synthetic GARCH series with known params -----------------------------
n = 900
w, a, b = 2e-6, 0.09, 0.88
var = w / (1 - a - b)
r = np.empty(n)
for i in range(n):
    z = rng.standard_t(6) / math.sqrt(6 / 4)
    e = math.sqrt(var) * z
    r[i] = e
    var = w + a * e * e + b * var

print("\n=== 1. forward_rv: aggregation, not sqrt-scaling ===")
frv = sc.forward_rv(r, horizon=20)
check("converged", frv.converged)
check("alpha+beta persistent", 0.80 < frv.alpha + frv.beta < 1.0, f"{frv.alpha+frv.beta:.3f}")
naive = sc.annualise_vol(frv.sigma_next)
check("h-day agg differs from sigma_1 scaling",
      abs(frv.rv_annual_pct - naive) > 1e-9,
      f"agg={frv.rv_annual_pct:.2f}% naive={naive:.2f}%")
check("var_path has h entries", frv.var_path.size == 20)
check("innovation pool populated", frv.innovations.size > 500, f"{frv.innovations.size}")
lr = frv.omega / (1 - frv.alpha - frv.beta)
rising = frv.var_path[-1] > frv.var_path[0] if frv.var_path[0] < lr else frv.var_path[-1] < frv.var_path[0]
check("forecast path mean-reverts toward long-run", rising,
      f"v1={frv.var_path[0]:.3e} vh={frv.var_path[-1]:.3e} lr={lr:.3e}")

print("\n=== 2. FHS preserves fat tails (the constant-sigma bug) ===")
paths = sc.fhs_paths(frv, horizon=20, n_paths=40000, drift=0.0, seed=1)
term = paths.sum(axis=1)
k_fhs = float(stats.kurtosis(term, fisher=False))
z = rng.choice(frv.innovations, size=(40000, 20))
flat = z * math.sqrt(float(np.mean(frv.var_path)))
k_flat = float(stats.kurtosis(flat.sum(axis=1), fisher=False))
check("FHS terminal kurtosis > constant-sigma", k_fhs > k_flat, f"fhs={k_fhs:.2f} flat={k_flat:.2f}")
check("constant-sigma collapses toward Gaussian", k_flat < 3.6, f"{k_flat:.2f}")
check("variance roughly matches forecast",
      abs(np.var(term) / frv.total_variance - 1) < 0.30,
      f"ratio={np.var(term)/frv.total_variance:.3f}")

print("\n=== 3. EM vs SD: the 0.798 identity ===")
gauss = rng.standard_normal((200000, 1))
ms = sc.empirical_move_stats(gauss)
check("E|X|/sd = 0.798 for a normal", abs(ms["mad_sd_ratio"] - sc.SQRT_2_OVER_PI) < 0.01,
      f"{ms['mad_sd_ratio']:.4f}")
check("median|X|/sd = 0.674 for a normal", abs(ms["median_abs"] / ms["sd"] - 0.6745) < 0.01,
      f"{ms['median_abs']/ms['sd']:.4f}")
ms_fat = sc.empirical_move_stats(paths)
check("fat tails LOWER E|X|/sd below 0.798", ms_fat["mad_sd_ratio"] < sc.SQRT_2_OVER_PI,
      f"{ms_fat['mad_sd_ratio']:.4f}")

print("\n=== 4. TFR: monotone in kurtosis, Gaussian = 1.0 ===")
tfr_g = sc.tail_fragility(gauss)
check("Gaussian TFR ~ 1.0", abs(tfr_g - 1.0) < 0.03, f"{tfr_g:.3f}")
tfrs = []
for nu in (12, 8, 6, 4):
    s = math.sqrt(nu / (nu - 2))
    t = rng.standard_t(nu, size=(120000, 1)) / s
    tfrs.append(sc.tail_fragility(t))
check("TFR increases as tails fatten", all(x < y for x, y in zip(tfrs, tfrs[1:])),
      " < ".join(f"{v:.3f}" for v in tfrs))
check("t(4) TFR clearly above Gaussian", tfrs[-1] > 1.20, f"{tfrs[-1]:.3f}")

print("\n=== 5. POP rises with fat tails (why it is excluded) ===")
bk = sc.SQRT_2_OVER_PI
pop_g = float((np.abs(gauss[:, 0]) < bk).mean())
t4 = rng.standard_t(4, size=200000) / math.sqrt(4 / 2)
pop_t = float((np.abs(t4) < bk).mean())
check("Gaussian straddle POP ~57.5%", abs(pop_g - 0.575) < 0.01, f"{pop_g:.3f}")
check("fat tails RAISE POP", pop_t > pop_g, f"t4={pop_t:.3f} > gauss={pop_g:.3f}")

print("\n=== 6. costs and margin ===")
st = sc.Structure(
    symbol="RELIANCE",
    legs=[sc.Leg("CE", 3000.0, 55.0, -1, bid=54.0, ask=56.0),
          sc.Leg("PE", 3000.0, 50.0, -1, bid=49.2, ask=50.8)],
    lot_size=250, ref_price=3000.0, iv_pct=28.0, kind="straddle")
check("gross credit", abs(st.gross_credit - 105.0) < 1e-9, f"{st.gross_credit}")
nc = sc.net_credit(st)
check("net credit < gross", nc < st.gross_credit, f"net={nc:.3f} gross={st.gross_credit}")
check("entry cost is material but sane",
      0.002 < (st.gross_credit - nc) / st.gross_credit < 0.10,
      f"{(st.gross_credit-nc)/st.gross_credit:.4f}")
ex_itm = sc.exit_costs(st, 400.0, exercised=True)
ex_cls = sc.exit_costs(st, 400.0, exercised=False)
check("exercise cost scales with intrinsic", sc.exit_costs(st, 4000.0, True) > 9 * ex_itm,
      f"400->{ex_itm:.2f} 4000->{sc.exit_costs(st,4000.0,True):.2f}")
check("exercise overtakes buy-to-close on deep ITM",
      sc.exit_costs(st, 4000.0, True) > sc.exit_costs(st, 4000.0, False),
      f"ex={sc.exit_costs(st,4000.0,True):.2f} cl={sc.exit_costs(st,4000.0,False):.2f}")
check("buy-to-close cheaper on small ITM (spread dominates)", ex_cls > ex_itm,
      f"close={ex_cls:.2f} exercise={ex_itm:.2f}")
m_leg = sc.leg_margin("RELIANCE", "CE", 3000, 3000, 55, 28.0)
m_combo = sc.combo_margin(st)
check("combo margin is NOT the sum of legs", m_combo < 2 * m_leg * 0.95,
      f"combo={m_combo:.1f} 2leg={2*m_leg:.1f}")
check("combo margin >= largest leg", m_combo >= m_leg, f"{m_combo:.1f} vs {m_leg:.1f}")

print("\n=== 7. credit-multiple vs EM-distance stop (portability) ===")
V0 = st.gross_credit
for k in (1.5, 2.0, 3.0):
    M = V0 * math.sqrt(k * k - 1)
    print(f"        k={k}x credit -> needs move of {M:7.1f} pts = {M/st.ref_price*100:5.2f}% of spot")
check("3x credit stop needs > 8% move on this straddle",
      V0 * math.sqrt(8) / st.ref_price > 0.08, f"{V0*math.sqrt(8)/st.ref_price*100:.2f}%")

print("\n=== 8. simulate_structure ===")
sim_hold = sc.simulate_structure(st, paths, spot=3000.0, stop_k_em=None)
sim_stop = sc.simulate_structure(st, paths, spot=3000.0, stop_k_em=1.25)
check("hold-to-expiry has zero stop rate", sim_hold.stop_rate == 0.0)
check("stop rule fires on some paths", 0.0 < sim_stop.stop_rate < 1.0, f"{sim_stop.stop_rate:.3f}")
check("stop truncates the loss tail", sim_stop.max_loss > sim_hold.max_loss,
      f"stop={sim_stop.max_loss:.1f} hold={sim_hold.max_loss:.1f}")
check("stop shortens mean hold", sim_stop.mean_hold_days < sim_hold.mean_hold_days,
      f"{sim_stop.mean_hold_days:.1f} vs {sim_hold.mean_hold_days:.1f}")
check("P&L is negatively skewed", sim_hold.skew < 0, f"{sim_hold.skew:.3f}")
check("cvar is worse than mean", sim_hold.cvar05 < sim_hold.ev)
print(f"        hold: ev={sim_hold.ev:7.2f} cvar5={sim_hold.cvar05:8.2f} "
      f"pop={sim_hold.pop:.3f} evpm={sim_hold.ev_per_margin:+.4f}")
print(f"        stop: ev={sim_stop.ev:7.2f} cvar5={sim_stop.cvar05:8.2f} "
      f"pop={sim_stop.pop:.3f} evpm={sim_stop.ev_per_margin:+.4f}")
print("        --- stop sweep (note: a NO-STOP arm is always included) ---")
for row in sc.sweep_stop(st, paths, 3000.0):
    print(f"        k={str(row['stop_k_em']):>5}  ev={row['ev']:8.2f}  "
          f"cvar5={row['cvar05']:9.2f}  stop_rate={row['stop_rate']:.3f}  "
          f"evpm={row['ev_per_margin']:+.4f}")

print("\n=== 9. scoring: ranks, no NaN propagation, direction ===")
feats = []
for i in range(30):
    feats.append(sc.Features(
        symbol=f"SYM{i}", ev_per_margin=rng.normal(0.02, 0.01),
        cvar05_per_margin=-abs(rng.normal(0.15, 0.05)),
        vrp_z=rng.normal(), ts_slope=rng.normal(0, 0.05),
        cushion_net=rng.uniform(0.6, 1.1),
        gamma_score=rng.choice([0.15, 0.35, 0.45, 0.85]),
        trend_eff=rng.uniform(0.05, 0.6), tfr=rng.uniform(1.0, 1.4)))
feats[0].vrp_z = float("nan")          # NaN must not kill the row
scored = sc.cross_sectional_score(feats)
check("all rows scored", len(scored) == 30)
check("NaN input does not NaN the score", np.isfinite(scored[0]["score"]), f"{scored[0]['score']}")
check("ranks are 1..n unique", sorted(s["score_rank"] for s in scored) == list(range(1, 31)))
top = max(scored, key=lambda s: s["score"])
check("top score has rank 1", top["score_rank"] == 1)
lo_tfr = [s for s in scored if s["tfr"] < 1.1]
if lo_tfr:
    check("low TFR gets high tfr rank-part",
          np.mean([s["rank_parts"]["tfr"] for s in lo_tfr]) > 0.5,
          f"{np.mean([s['rank_parts']['tfr'] for s in lo_tfr]):.3f}")

print("\n=== 10. universe demeaning kills the common factor ===")
common, idio = 1.4, rng.normal(0, 0.5, 60)
z_raw = common + idio
z_dm = sc.demean_universe(z_raw)
check("raw z nearly all same sign (common factor)", (z_raw > 0).mean() > 0.95, f"{(z_raw>0).mean():.3f}")
check("demeaned z is centred", abs(np.median(z_dm)) < 1e-9, f"{np.median(z_dm):.2e}")
check("demeaning preserves cross-sectional order",
      stats.spearmanr(z_raw, z_dm).statistic > 0.999)

print("\n=== 11. gates ===")
cfg = sc.GateConfig()
ok, why = sc.apply_gates(st, dte=25, cfg=cfg, event_in_window=False, n_strikes=15, confidence="high")
check("clean candidate passes", ok, str(why))
ok2, why2 = sc.apply_gates(st, dte=25, cfg=cfg, event_in_window=True, n_strikes=15, confidence="high")
check("event in window rejects", not ok2 and "event in window" in why2)
wide = sc.Structure("X", [sc.Leg("CE", 100, 5, -1, bid=3.0, ask=7.0),
                          sc.Leg("PE", 100, 5, -1, bid=3.0, ask=7.0)],
                    100, 100.0, 40.0)
ok3, why3 = sc.apply_gates(wide, dte=25, cfg=cfg, event_in_window=False)
check("wide spread rejects", not ok3, str(why3))
ok4, _ = sc.apply_gates(st, 25, cfg, event_in_window=None)
check("missing event data passes when not required", ok4)
cfg2 = sc.GateConfig(require_event_data=True)
ok5, why5 = sc.apply_gates(st, 25, cfg2, event_in_window=None)
check("missing event data rejects when required", not ok5 and "no event data" in why5)

print("\n=== 12. forecast diagnostics ===")
f_ = rng.uniform(0.01, 0.05, 200)
a_ = 0.002 + 0.9 * f_ + rng.normal(0, 0.002, 200)
mz = sc.mincer_zarnowitz(f_, a_)
check("MZ recovers slope", abs(mz["beta"] - 0.9) < 0.1, f"beta={mz['beta']:.3f}")
q_good = sc.qlike(f_ ** 2, (f_ ** 2) * rng.lognormal(0, 0.2, 200))
q_bad = sc.qlike(f_ ** 2 * 2.5, (f_ ** 2) * rng.lognormal(0, 0.2, 200))
check("QLIKE penalises biased forecast", q_bad > q_good, f"good={q_good:.4f} bad={q_bad:.4f}")

print("\n=== 13. path efficiency ===")
check("trending -> efficiency ~1", sc.path_efficiency(np.full(20, 0.004)) > 0.99)
check("round trip -> efficiency ~0", sc.path_efficiency(np.array([0.004, -0.004] * 10)) < 0.01)

print("\n" + "=" * 62)
print(f"  {'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + ', '.join(fails)}")
print("=" * 62)


def test_all_properties():
    """pytest entry point."""
    assert not fails, f"failed: {fails}"
