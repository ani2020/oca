"""Vol screener endpoints — model-ranked straddle/strangle, and model tracking.

Reads the NIGHTLY PREDICTION LOG (`vol_prediction`), not a live recomputation.
That is deliberate. The log is what was actually predicted and frozen last
night; recomputing on request would mean the page shows one ranking while the
track record stores another, and every later comparison would be grading a
model against numbers it never emitted. A "recompute" action can be added
later for intraday use, but it must write to the log, not bypass it.

These endpoints are ADDITIVE. `screener.py` and its straddle/strangle tabs are
untouched and stay in the UI. The new model has 71 expiries of backtest and,
as of writing, one day of forward evidence -- so the existing view is the
control, and the two are compared on identical candidates rather than one
replacing the other on the strength of a backtest.

The ranking model, for reference (see straddle_core.RANK_DIRECTION):
    ev_per_margin   IC 0.1250  simulator EV per rupee of margin
    trend_eff       IC 0.0710  prior 20d path efficiency, HIGHER is better
    2-term score    IC 0.1307  t 8.24, 55/71 expiries positive
Ten other candidates were measured and cut; nine of them had IC <= 0.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from ..db import qdf, to_records, safe_response

import pandas as pd

try:  # oc/ is on sys.path once helpers is imported; fall back if not
    import straddle_core as _sc
    _UNIVERSE_VRP_MIN = float(_sc.UNIVERSE_VRP_MIN)
except Exception:  # pragma: no cover
    _UNIVERSE_VRP_MIN = 1.05


def _has_universe_cols() -> bool:
    """universe_* columns appear on build_vol_predictions' first run after
    2026-10-04; until then select NULLs so the page does not break."""
    try:
        return int(qdf("SELECT count(*) AS n FROM duckdb_columns() "
                       "WHERE table_name = 'vol_prediction' "
                       "AND column_name = 'universe_gate'")["n"].iloc[0]) > 0
    except Exception:
        return False

router = APIRouter()

#: legacy sortable columns, logged alongside the model score so the two can be
#: compared on identical candidates. The existing tab has no single score --
#: it returns metrics and the UI sorts -- so each sortable column is treated as
#: its own candidate ranking rule.
LEGACY_RANKERS = ["legacy_rom", "legacy_be_vs_hv", "legacy_vrp"]


@router.get("/api/vol_screener/straddle")
def vol_screener_straddle(
    asof:        Optional[str] = Query(None, description="YYYY-MM-DD; default latest"),
    expiry_rank: int           = Query(0),
    gated_only:  bool          = Query(True),
    limit:       int           = Query(200),
):
    """Model-ranked ATM straddles from last night's prediction log.

    gated_only defaults True for the trading view, but the rejected rows ARE in
    the log with their reasons -- pass gated_only=false to see them. Keeping
    rejects visible is the only way to find out whether a gate is throwing away
    good trades; a screener that silently drops them can never be audited.
    """
    day = asof or qdf(
        "SELECT max(asof_date) AS d FROM vol_prediction WHERE structure='straddle'"
    )["d"].iloc[0]
    if day is None:
        return safe_response({"asof": None, "rows": [], "note": "log is empty"})

    gate = "AND p.gate_pass" if gated_only else ""
    has_u = _has_universe_cols()
    ucols = ("p.universe_vrp_median, p.universe_gate," if has_u else
             "NULL::DOUBLE AS universe_vrp_median, NULL::BOOLEAN AS universe_gate,")
    # Month-level premium gate (one value per asof/expiry_rank). Separate from the
    # stock gates: gated_only filters STOCK gates only, so a failing month still
    # shows its otherwise-clean names, flagged, instead of an empty page.
    universe = {"median_iv_over_rv": None, "threshold": _UNIVERSE_VRP_MIN, "pass": None}
    if has_u:
        try:
            u = qdf("""
                SELECT any_value(universe_vrp_median) AS med, any_value(universe_gate) AS ok
                FROM vol_prediction
                WHERE asof_date = ? AND structure = 'straddle' AND expiry_rank = ?
            """, [str(day)[:10], expiry_rank])
            med, okv = u["med"].iloc[0], u["ok"].iloc[0]
            universe["median_iv_over_rv"] = None if med is None or pd.isna(med) else float(med)
            universe["pass"] = None if okv is None or pd.isna(okv) else bool(okv)
        except Exception:
            pass
    df = qdf(
        f"""
        SELECT p.symbol, p.expiry, p.dte, p.expiry_rank,
               p.spot, p.fut_price, p.atm_strike, p.lot,
               p.ce_ltp, p.pe_ltp, p.gross_credit, p.net_credit, p.margin,
               p.atm_iv, p.hv20, p.rv_fcst_pct, p.iv_pctile, p.iv_rank,
               p.total_vol_in_range, p.spread_frac, p.spread_cost,
               -- model
               p.score, p.score_rank, p.ev_per_margin, p.trend_eff,
               p.sim_cvar05_per_margin, p.sim_pop, p.sim_tfr,
               -- legacy sortables + their ranks, for side-by-side
               p.legacy_rom, p.legacy_rom_rank,
               p.legacy_be_vs_hv, p.legacy_be_vs_hv_rank,
               p.legacy_vrp, p.legacy_vrp_rank, p.legacy_iv_pctile_rank,
               -- decision + provenance
               p.gate_pass, p.gate_reasons, p.event_in_window,
               {ucols}
               p.model_version, p.data_source
        FROM vol_prediction p
        WHERE p.asof_date = ? AND p.structure = 'straddle'
          AND p.expiry_rank = ? {gate}
        ORDER BY p.score DESC NULLS LAST
        LIMIT {int(limit)}
        """,
        [str(day)[:10], expiry_rank],
    )
    if df.empty:
        n_total = int(qdf(
            "SELECT count(*) AS n FROM vol_prediction WHERE asof_date = ? "
            "AND structure = 'straddle' AND expiry_rank = ?",
            [str(day)[:10], expiry_rank],
        )["n"].iloc[0])
        return safe_response({"asof": str(day)[:10], "expiry_rank": expiry_rank,
                              "n_total": n_total, "rows": [], "universe": universe})

    rows = to_records(df)
    n_total = int(qdf(
        "SELECT count(*) AS n FROM vol_prediction WHERE asof_date = ? "
        "AND structure = 'straddle' AND expiry_rank = ?",
        [str(day)[:10], expiry_rank],
    )["n"].iloc[0])
    # Disagreement vs the legacy view, per row. This is the column worth
    # looking at: on the first live day the model's top-20 shared only 2-3
    # names with each legacy sort, and rank correlation was -0.06 vs RoM. The
    # two are near-independent, which is what makes running both informative --
    # if they agreed there would be nothing to learn from the comparison.
    for r in rows:
        nr, lr = r.get("score_rank"), r.get("legacy_rom_rank")
        r["rank_delta_vs_rom"] = (int(lr) - int(nr)) if nr and lr else None

    return safe_response({
        "asof": str(day)[:10],
        "expiry_rank": expiry_rank,
        "gated_only": gated_only,
        "n": len(rows),
        "n_total": n_total,
        "universe": universe,
        "rows": rows,
        "model": {
            "terms": ["ev_per_margin", "trend_eff"],
            "backtest_ic": 0.1307, "backtest_t": 8.24,
            "backtest_expiries": 71, "backtest_net_rom_pct": 2.21,
            "note": "Backtest only. Forward evidence starts 2026-09-18 and is "
                    "not directly comparable: the backtest ran on "
                    "exposure_hist (EOD-reconstructed), this runs on "
                    "exposure_eod (live). atm_iv correlates 0.979 between "
                    "them, but flip_norm_distance only 0.48.",
        },
    })


@router.get("/api/vol_screener/gate_summary")
def gate_summary(
    asof:        Optional[str] = Query(None),
    expiry_rank: int           = Query(0),
):
    """Why candidates were rejected, for the given day.

    Surfaced because a gate is a silent filter otherwise. Measured on the first
    live day: rank 0 rejected 8 of 159 (mostly spread/credit), rank 1 rejected
    54 of 92 and every one was the volume floor -- far-month chains are simply
    thin, and that is the gate working rather than failing.
    """
    day = asof or qdf(
        "SELECT max(asof_date) AS d FROM vol_prediction WHERE structure='straddle'"
    )["d"].iloc[0]
    df = qdf(
        """
        SELECT gate_reasons, count(*) AS n
        FROM vol_prediction
        WHERE asof_date = ? AND structure='straddle' AND expiry_rank = ?
          AND NOT gate_pass AND gate_reasons <> ''
        GROUP BY 1 ORDER BY 2 DESC
        """,
        [str(day)[:10], expiry_rank],
    )
    tot = qdf(
        """
        SELECT count(*) AS total, count(*) FILTER (WHERE gate_pass) AS passed
        FROM vol_prediction
        WHERE asof_date = ? AND structure='straddle' AND expiry_rank = ?
        """,
        [str(day)[:10], expiry_rank],
    )
    return safe_response({
        "asof": str(day)[:10], "expiry_rank": expiry_rank,
        "total": int(tot["total"].iloc[0]) if not tot.empty else 0,
        "passed": int(tot["passed"].iloc[0]) if not tot.empty else 0,
        "reasons": to_records(df) if not df.empty else [],
    })


@router.get("/api/vol_screener/compare")
def vol_screener_compare(min_rows: int = Query(50)):
    """Forward track record: model score vs every legacy sortable column.

    Paired -- same candidates, same expiries, only the ranking rule differs, so
    a bad month is attributable to the model rather than to the month.

    DO NOT read this until roughly 20-30 expiries have resolved (about two
    years at monthly cadence). Below that the per-expiry IC standard error
    swamps any plausible difference, and an early lead means nothing. `periods`
    is returned so the caller can refuse to draw a conclusion.
    """
    df = qdf(
        """
        SELECT p.expiry, p.score, p.ev_per_margin, p.trend_eff,
               p.legacy_rom, p.legacy_be_vs_hv, p.legacy_vrp,
               o.realised_pnl_per_margin AS y
        FROM vol_prediction p JOIN vol_outcome o USING (pred_id)
        WHERE p.gate_pass AND o.realised_pnl_per_margin IS NOT NULL
        """
    )
    if df.empty or len(df) < min_rows:
        return safe_response({
            "ready": False, "n": int(len(df)), "periods": 0,
            "note": f"{len(df)} resolved rows; need at least {min_rows}, and "
                    f"20-30 expiries before the comparison means anything.",
        })

    import numpy as np
    from scipy.stats import spearmanr

    out = []
    for col in ["score", "ev_per_margin", "trend_eff"] + LEGACY_RANKERS:
        per = []
        for _, g in df.groupby("expiry"):
            a = g[col].to_numpy(float)
            b = g["y"].to_numpy(float)
            m = np.isfinite(a) & np.isfinite(b)
            # a constant column cannot rank anything; skip rather than emit NaN
            if m.sum() >= 8 and a[m].min() < a[m].max():
                per.append(float(spearmanr(a[m], b[m]).statistic))
        per = np.array([x for x in per if np.isfinite(x)])
        if per.size < 2:
            continue
        se = per.std(ddof=1) / np.sqrt(per.size)
        out.append({
            "ranker": col,
            "is_model": col in ("score", "ev_per_margin", "trend_eff"),
            "ic": round(float(per.mean()), 4),
            "t": round(float(per.mean() / se), 2) if se > 0 else None,
            "periods": int(per.size),
            "positive": int((per > 0).sum()),
        })
    out.sort(key=lambda r: -r["ic"])
    periods = max((r["periods"] for r in out), default=0)
    return safe_response({
        "ready": periods >= 20,
        "n": int(len(df)), "periods": periods, "rankers": out,
        "note": ("Comparable: same candidates, same expiries." if periods >= 20
                 else f"Only {periods} resolved expiries -- accumulating. "
                      f"Do not retire the legacy view on this."),
    })
