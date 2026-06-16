"""Divergence endpoint."""
from __future__ import annotations
from datetime import date, datetime, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from ..db import qdf, tbl, to_records, safe_response, _safe, latest_ts
from .. import config

router = APIRouter()

@router.get("/api/divergence")
def divergence(
    mode:        str = Query("snapshot"),   # "snapshot" | "intraday"
    filter_type: str = Query("all"),
    top_n:       int = Query(30),
    min_spot_chg: float = Query(0.03),      # min |spot % change| to flag
    min_prem_chg: float = Query(0.5),       # min |premium % change| to flag
):
    """
    Identify divergences between spot/futures price moves and option premium moves.

    Mode 'snapshot': Compare two most recent timestamps per symbol.
    Mode 'intraday': Compare first timestamp of today vs latest per symbol.

    Flags:
      BULL_HEDGE   : Spot UP  + PE premium UP  (puts bought on rally — bearish hedge)
      BEAR_SQUEEZE : Spot DOWN + CE premium UP  (calls bought on dip — bear squeeze risk)
      IV_SPIKE     : Spot FLAT + IV spike       (event anticipation, not delta-driven)
      SMART_SELL   : Spot UP  + CE premium DOWN (calls being sold on rally — ceiling signal)
      SMART_BUY    : Spot DOWN + PE premium DOWN (puts being sold on dip — floor signal)
    """
    idx_list = ", ".join(f"'{s}'" for s in config.NSE_INDICES)
    sym_filter = (
        f"AND symbol IN ({idx_list})"       if filter_type == "index"
        else f"AND symbol NOT IN ({idx_list})" if filter_type == "stock"
        else ""
    )

    if mode == "intraday":
        # First timestamp today vs latest timestamp today per symbol
        df_ref = qdf(
            f"""
            WITH day_bounds AS (
                SELECT symbol,
                       MIN(timestamp) AS ts_open,
                       MAX(timestamp) AS ts_now
                FROM {tbl()}
                WHERE CAST(timestamp AS DATE) = (
                          SELECT MAX(CAST(timestamp AS DATE)) FROM {tbl()}
                      )
                {sym_filter}
                GROUP BY symbol
                HAVING MIN(timestamp) != MAX(timestamp)
            ),
            near_exp AS (
                -- per-symbol nearest (front) expiry — pin ATM premium to ONE expiry
                SELECT symbol, MIN(expiry) AS expiry
                FROM {tbl()}
                WHERE expiry >= CURRENT_DATE
                GROUP BY symbol
            )
            SELECT
                n.symbol,
                STRFTIME(n.timestamp, '%Y-%m-%d %H:%M') AS ts_now,
                STRFTIME(o.timestamp, '%Y-%m-%d %H:%M') AS ts_open,
                AVG(n.underlying_price) - AVG(o.underlying_price)     AS spot_chg,
                AVG(o.underlying_price)                                AS spot_open,
                AVG(n.underlying_price)                                AS spot_now,
                -- ATM CE/PE (distance_from_atm = 0)
                AVG(CASE WHEN n.distance_from_atm = 0 THEN n.ce_ltp ELSE NULL END) AS ce_ltp_now,
                AVG(CASE WHEN o.distance_from_atm = 0 THEN o.ce_ltp ELSE NULL END) AS ce_ltp_open,
                AVG(CASE WHEN n.distance_from_atm = 0 THEN n.pe_ltp ELSE NULL END) AS pe_ltp_now,
                AVG(CASE WHEN o.distance_from_atm = 0 THEN o.pe_ltp ELSE NULL END) AS pe_ltp_open,
                AVG(CASE WHEN n.distance_from_atm = 0 THEN n.ce_iv  ELSE NULL END) AS ce_iv_now,
                AVG(CASE WHEN o.distance_from_atm = 0 THEN o.ce_iv  ELSE NULL END) AS ce_iv_open,
                AVG(CASE WHEN n.distance_from_atm = 0 THEN n.pe_iv  ELSE NULL END) AS pe_iv_now,
                AVG(CASE WHEN o.distance_from_atm = 0 THEN o.pe_iv  ELSE NULL END) AS pe_iv_open,
                AVG(CASE WHEN n.distance_from_atm = 0 THEN n.net_flow ELSE NULL END) AS net_flow
            FROM {tbl()} n
            JOIN day_bounds d ON n.symbol = d.symbol AND n.timestamp = d.ts_now
            JOIN near_exp ne  ON n.symbol = ne.symbol AND n.expiry = ne.expiry
            JOIN {tbl()} o    ON o.symbol = d.symbol AND o.timestamp = d.ts_open
                              AND o.expiry = ne.expiry
            GROUP BY n.symbol, n.timestamp, o.timestamp
            """
        )
    else:
        # Compare two most recent timestamps per symbol.
        # Use a CTE to get exact ts_now and ts_prev per symbol first,
        # then join only those two rows — avoids a full cross-join.
        df_ref = qdf(
            f"""
            WITH near_exp AS (
                -- per-symbol nearest (front) expiry — pin ATM premium to ONE expiry
                SELECT symbol, MIN(expiry) AS expiry
                FROM {tbl()}
                WHERE expiry >= CURRENT_DATE {sym_filter}
                GROUP BY symbol
            ),
            sym_ts AS (
                SELECT symbol,
                       MAX(timestamp) AS ts_now,
                       MIN(timestamp) AS ts_prev
                FROM (
                    SELECT symbol, timestamp,
                           DENSE_RANK() OVER (PARTITION BY symbol ORDER BY timestamp DESC) AS rk
                    FROM {tbl()}
                    WHERE expiry >= CURRENT_DATE {sym_filter}
                ) ranked
                WHERE rk <= 2
                GROUP BY symbol
                HAVING COUNT(DISTINCT timestamp) >= 2
            ),
            snap_now AS (
                SELECT t.symbol,
                       AVG(t.underlying_price) AS spot_now,
                       AVG(CASE WHEN t.distance_from_atm=0 THEN t.ce_ltp   END) AS ce_ltp_now,
                       AVG(CASE WHEN t.distance_from_atm=0 THEN t.pe_ltp   END) AS pe_ltp_now,
                       AVG(CASE WHEN t.distance_from_atm=0 THEN t.ce_iv    END) AS ce_iv_now,
                       AVG(CASE WHEN t.distance_from_atm=0 THEN t.pe_iv    END) AS pe_iv_now,
                       AVG(CASE WHEN t.distance_from_atm=0 THEN t.net_flow END) AS net_flow,
                       STRFTIME(MAX(t.timestamp), '%Y-%m-%d %H:%M') AS ts_now_str
                FROM {tbl()} t
                JOIN sym_ts s   ON t.symbol=s.symbol AND t.timestamp=s.ts_now
                JOIN near_exp ne ON t.symbol=ne.symbol AND t.expiry=ne.expiry
                GROUP BY t.symbol
            ),
            snap_prev AS (
                SELECT t.symbol,
                       AVG(t.underlying_price) AS spot_prev,
                       AVG(CASE WHEN t.distance_from_atm=0 THEN t.ce_ltp END) AS ce_ltp_prev,
                       AVG(CASE WHEN t.distance_from_atm=0 THEN t.pe_ltp END) AS pe_ltp_prev,
                       AVG(CASE WHEN t.distance_from_atm=0 THEN t.ce_iv  END) AS ce_iv_prev,
                       AVG(CASE WHEN t.distance_from_atm=0 THEN t.pe_iv  END) AS pe_iv_prev,
                       STRFTIME(MAX(t.timestamp), '%Y-%m-%d %H:%M') AS ts_prev_str
                FROM {tbl()} t
                JOIN sym_ts s   ON t.symbol=s.symbol AND t.timestamp=s.ts_prev
                JOIN near_exp ne ON t.symbol=ne.symbol AND t.expiry=ne.expiry
                GROUP BY t.symbol
            )
            SELECT n.symbol,
                   n.ts_now_str  AS ts_now,
                   p.ts_prev_str AS ts_open,
                   n.spot_now - p.spot_prev  AS spot_chg,
                   p.spot_prev               AS spot_open,
                   n.spot_now                AS spot_now,
                   n.ce_ltp_now, p.ce_ltp_prev AS ce_ltp_open,
                   n.pe_ltp_now, p.pe_ltp_prev AS pe_ltp_open,
                   n.ce_iv_now,  p.ce_iv_prev  AS ce_iv_open,
                   n.pe_iv_now,  p.pe_iv_prev  AS pe_iv_open,
                   n.net_flow
            FROM snap_now n
            JOIN snap_prev p ON n.symbol = p.symbol
            """
        )

    if df_ref.empty:
        return []

    rows = []
    for _, r in df_ref.iterrows():
        spot_open = float(r.get("spot_open") or 0)
        spot_now  = float(r.get("spot_now")  or 0)
        if spot_open <= 0:
            continue
        spot_pct  = (spot_now - spot_open) / spot_open * 100

        ce_open = float(r.get("ce_ltp_open") or 0)
        ce_now  = float(r.get("ce_ltp_now")  or 0)
        pe_open = float(r.get("pe_ltp_open") or 0)
        pe_now  = float(r.get("pe_ltp_now")  or 0)

        ce_pct  = (ce_now - ce_open) / ce_open * 100 if ce_open > 0 else 0
        pe_pct  = (pe_now - pe_open) / pe_open * 100 if pe_open > 0 else 0

        ce_iv_chg = float((r.get("ce_iv_now") or 0)) - float((r.get("ce_iv_open") or 0))
        pe_iv_chg = float((r.get("pe_iv_now") or 0)) - float((r.get("pe_iv_open") or 0))

        # Classify divergence
        signal = None
        magnitude = 0.0
        spot_flat = abs(spot_pct) < min_spot_chg

        if not spot_flat:
            if spot_pct > min_spot_chg and pe_pct > min_prem_chg:
                signal = "BULL_HEDGE"
                magnitude = abs(spot_pct) + abs(pe_pct)
            elif spot_pct < -min_spot_chg and ce_pct > min_prem_chg:
                signal = "BEAR_SQUEEZE"
                magnitude = abs(spot_pct) + abs(ce_pct)
            elif spot_pct > min_spot_chg and ce_pct < -min_prem_chg:
                signal = "SMART_SELL"
                magnitude = abs(spot_pct) + abs(ce_pct)
            elif spot_pct < -min_spot_chg and pe_pct < -min_prem_chg:
                signal = "SMART_BUY"
                magnitude = abs(spot_pct) + abs(pe_pct)
        else:
            # Spot flat but IV spiked
            avg_iv_chg = (abs(ce_iv_chg) + abs(pe_iv_chg)) / 2
            if avg_iv_chg >= 1.0:  # at least 1% IV move
                signal = "IV_SPIKE"
                magnitude = avg_iv_chg

        if signal is None:
            continue

        rows.append({
            "symbol":     r["symbol"],
            "signal":     signal,
            "magnitude":  round(magnitude, 3),
            "spot_pct":   round(spot_pct,  3),
            "ce_pct":     round(ce_pct,    3),
            "pe_pct":     round(pe_pct,    3),
            "ce_iv_chg":  round(ce_iv_chg, 3),
            "pe_iv_chg":  round(pe_iv_chg, 3),
            "spot_now":   round(spot_now,  2),
            "ce_ltp_now": round(ce_now,    2),
            "pe_ltp_now": round(pe_now,    2),
            "net_flow":   round(float(r.get("net_flow") or 0), 2),
            "ts_now":     r.get("ts_now", ""),
            "ts_ref":     r.get("ts_open", ""),
        })

    rows.sort(key=lambda x: x["magnitude"], reverse=True)
    return safe_response(rows[:top_n])


