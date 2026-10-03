"""Market-level fragility gauge.

Query-only read of `market_state_eod`, which the nightly batch maintains via
market_state.refresh(). Never computes on load; degrades to
{"available": false} if the table is absent so the UI can hide the strip.

Kept as its own module (rather than folded into /api/overview_meta) so the
strip can be dropped onto any screen and loads independently of the rest of
the Overview payload.

WHAT THE NUMBERS MEAN — see market_state.py for the full derivation and the
rejected-metrics list. Short version:

  ngf_avg   mean neg_gamma_fraction across single-stock F&O names, rank=0.
            The share of the strike grid where dealers are short gamma and
            therefore must trade WITH the move rather than against it.
            Correlates +51.3 with NIFTY's forward 5d realised vol and only
            +27.2 with the trailing 5d — it leads. NIFTY's own ATM IV is
            61.8 forward / 64.3 trailing, i.e. essentially coincident.

  vol_pay   *** RETRACTED 2026-08-25 — NOT RETURNED BY THIS ENDPOINT. ***
            realised forward 5d vol / implied daily vol (atm_iv / sqrt(243)).
            This docstring previously called it "the actionable column" and
            quoted by-state values of 0.762 / 0.775 / 0.920 / 1.069. Those four
            numbers are the ones market_state.py retracts: the spread is a
            calendar/DTE-cycle confound, not a positioning edge, and the claim
            that a FRAGILE_RICH day implies negative expectancy for a premium
            seller is NOT SUPPORTED. The producer stopped publishing it and the
            frontend stopped drawing it; this module was the last place still
            computing and shipping it, so it is gone from state_stats and
            current_stats too. Do not reinstate without a fresh within-DTE-band
            control. See the retraction note in market_state.py.

This endpoint returns TODAY's reading plus the HISTORICAL by-state averages of
the surviving columns (ngf, IV, realised vol). It does not attempt to tell you
what a state "pays".
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from ..db import qdf, safe_response

router = APIRouter()

TABLE = "market_state_eod"


@router.get("/api/market_state")
def market_state(history: int = Query(60, ge=0, le=250,
                                      description="sessions of series to return")):
    out = {
        "available": False,
        "today": None,
        "display": None,
        "stale": False,
        "stale_reason": None,
        "prev": None,
        "state_stats": [],
        "current_stats": None,
        "series": [],
        "n_history": 0,
    }

    try:
        rows = qdf(f"""
            SELECT date, n_symbols, ngf_avg, ngf_hi_frac, neg_regime_frac,
                   pos_regime_frac, nifty_spot, nifty_iv, trail_vol5, fwd_vol5,
                   ngf_pctile, iv_pctile, fragility_gap, near_dte, ngf_adj,
                   coverage_ok, state
            FROM {TABLE}
            ORDER BY date DESC LIMIT 2
        """)
    except Exception:
        # table not created yet — UI hides the strip
        return safe_response(out)

    if rows.empty:
        return safe_response(out)

    out["available"] = True
    out["today"] = {k: _n(v) for k, v in rows.iloc[0].to_dict().items()}

    # Which row to actually render. market_state.refresh() nulls `state` when the
    # row is not comparable to its own history — thin symbol coverage, or not
    # enough history for percentiles. Do NOT hide the strip in that case: fall
    # back to the most recent usable reading and mark it stale. Positioning does
    # not reset overnight, so a flagged day-old reading is far more useful than a
    # blank space, and hiding it silently loses the warning as well as the signal.
    out["display"] = out["today"]
    if not out["today"].get("state"):
        out["stale"] = True
        out["stale_reason"] = ("low_coverage"
                               if out["today"].get("coverage_ok") is False
                               else "insufficient_history")
        try:
            good = qdf(f"""
                SELECT date, n_symbols, ngf_avg, ngf_hi_frac, neg_regime_frac,
                       pos_regime_frac, nifty_spot, nifty_iv, trail_vol5, fwd_vol5,
                       ngf_pctile, iv_pctile, fragility_gap, near_dte, ngf_adj,
                       coverage_ok, state
                FROM {TABLE}
                WHERE state IS NOT NULL
                ORDER BY date DESC LIMIT 1
            """)
            out["display"] = ({k: _n(v) for k, v in good.iloc[0].to_dict().items()}
                              if not good.empty else None)
        except Exception:
            out["display"] = None

    if len(rows) > 1:
        p = rows.iloc[1]
        out["prev"] = {"date": _n(p["date"]), "state": _n(p["state"]),
                       "ngf_avg": _n(p["ngf_avg"]),
                       "ngf_pctile": _n(p["ngf_pctile"]),
                       "iv_pctile": _n(p["iv_pctile"])}

    # Historical behaviour of each state. vol_pay is deliberately absent — see
    # the retraction note at the top of this module.
    try:
        st = qdf(f"""
            SELECT state,
                   COUNT(*)                                   AS n,
                   AVG(ngf_avg)                               AS ngf_avg,
                   AVG(nifty_iv)                              AS nifty_iv,
                   AVG(trail_vol5)                            AS trail_vol5,
                   AVG(fwd_vol5)                              AS fwd_vol5
            FROM {TABLE}
            WHERE state IS NOT NULL AND fwd_vol5 IS NOT NULL
            GROUP BY state ORDER BY ngf_avg
        """)
        out["state_stats"] = [{k: _n(v) for k, v in r.items()}
                              for r in st.to_dict("records")]
        cur = out["display"].get("state") if out["display"] else None
        for s in out["state_stats"]:
            if s["state"] == cur:
                out["current_stats"] = s
                break
    except Exception:
        pass

    if history:
        try:
            ser = qdf(f"""
                SELECT date, ngf_avg, ngf_pctile, iv_pctile, nifty_iv,
                       trail_vol5, state
                FROM {TABLE} ORDER BY date DESC LIMIT {int(history)}
            """)
            out["series"] = [{k: _n(v) for k, v in r.items()}
                             for r in ser.iloc[::-1].to_dict("records")]
        except Exception:
            pass

    try:
        tot = qdf(f"SELECT COUNT(*) AS n FROM {TABLE}")
        out["n_history"] = int(tot["n"].iloc[0]) if not tot.empty else 0
    except Exception:
        pass

    return safe_response(out)


def _n(v):
    """Dates to ISO strings, NaN to None; safe_response handles the rest."""
    import datetime as _dt
    import math
    if v is None:
        return None
    if isinstance(v, (_dt.date, _dt.datetime)):
        return str(v)[:10]
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    try:
        import numpy as _np
        if isinstance(v, _np.generic):
            v = v.item()
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                return None
    except Exception:
        pass
    return v
