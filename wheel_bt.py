"""
wheel_bt -- short-premium (Option A) / wheel backtest on index-constituent stocks.

Spec: "Wheel / Premium-Selling Backtest Spec" (Claude Doc). Phase 1 = Option A:
short OTM puts and naked calls, always closed before expiry, sized by exact
exchange SPAN margin.

Data (read-only)
----------------
  oc.duckdb        index_constituents_hist (point-in-time universe; month-end lists
                   from NSE Indices, each applied from the next trading day) or
                   index_constituents (today's list; universe='current')
  bhavcopy.duckdb  options_eod (entry price, traded close, lot)
                   span_contract / span_und / span_ingest_log (margin, delta,
                       exchange price for every contract every day)
                   cash_returns_adj (CA-adjusted daily move, is_ca)
                   corporate_actions, corporate_events (CA / results blackouts)

Two SQL passes, then a pure-python day loop:
  1. candidates: per (day, symbol, side) the furthest-OTM TRADED strike with
     ROM >= rom_target and |delta| <= delta_cap, for the monthly contract with
     dte_lo..dte_hi calendar days left. ROM = premium*lot / margin,
     margin = SPAN short_scan*lot*broker_mult + ELM*underlying*lot.
  2. paths: daily rows for only the chosen contracts, built on span_contract
     (an exchange price for every contract every day, traded or not), with
     the bhavcopy close/volume joined where it traded.

Prices (price_basis='close', since 22 Oct 2026): fills use the bhavcopy CLOSE
(ClsPric: traded, last-30-minute VWAP, always inside the day's range); exits on
untraded days use the SPAN price plus untraded_slip. The earlier SETTLE basis
equals the SPAN theoretical price when a contract did not trade late -- 11% of
the strategy's entries, ~2x the day high -- and ROM / ranking picked exactly
those; runs on it carry the 'settle_fills' flag. Entry needs min_entry_volume
contracts traded that day. Spec and results log: the "Wheel / Premium-Selling
Backtest Spec" doc (tabs: Spec (26 Oct), Results log, Seasonality and IV events).

Events (results blackout): by default EVERY board meeting except AGM/EGM is
treated as a results-type event (event_scope='board'), because NSE labels
some results meetings 'Dividend' or 'Other business matters' (JSWSTEEL
22-Jul-2022, TECHM 23-Oct-2020, PFC 27-May-2023). pit_events uses only
meetings already intimated to the exchange on the day (announced_at; median
lead 14 days, 18% under a week), so the gate never knows a date early.
require_event_coverage skips a symbol until ~100 days before its first
results record (NESTLEIND has none before Oct-2023), so a missing calendar
is never mistaken for an event-free one.

Liquidity (plan item 0): entry needs >= min_entry_volume contracts traded and
>= min_entry_oi contracts of OI; multi-lot sizing needs >= multi_lot_min_volume.
Costs: half the measured 15:40 spread for the strike's volume bucket on each
side (cost_model='spread'), replacing the flat 10% that undercharged thin strikes.

Portfolio risk (plan item P), all point-in-time:
  stress_cap  every day the book is repriced (Black-76, IV backed out of the
              mark) under NIFTY moves of -10/-15/-20% and +10/+15%, each stock
              moving beta x NIFTY with IV scaled up; a new trade must keep the
              worst scenario loss within stress_cap x cash equity (pct sizing
              shrinks lots to fit, fixed sizing skips).
  stress_trim_at  the cap gates entries only; past this x cap, close the biggest
              contributors to the worst scenario until back at the cap.
  rank_by     'stress' ranks entries by premium per rupee of worst-scenario
              loss, since the stress budget, not margin, is now what binds.
  stress_idio single-stock shocks (move, IV mult) on one name at a time, not
              beta-scaled (--idio 0.20:1.3). Counted in rank_by='stress' and capped
              per name at stress_idio_cap (--idio-cap; default the stress cap).
  brake       (off by default) no new puts while India VIX > 25, VIX is 30%
              above its 5-session low, or NIFTY is 5% below its 10-session high.
  dd_halve_at pct sizing halves while equity is that far (x cash) below its peak.
  hedge       long NIFTY put ~hedge_otm below spot, sized to hedge_ratio x the
              beta-weighted short-put notional, rolled hedge_roll_td sessions
              before expiry (backtest only; fractional index units).
Plan items 1, 2, 4: ladder_entries / ladder_min_gap_days, exit_hurdle (hard) and
hurdle_swap (soft: close only to make room for a waiting entry), call_trend_dist
/ call_trend_ret (no new calls after a run-up; the trend-break exit was dropped).

Corporate actions: blackout. No entry if a CA ex-date falls inside the
contract's life; a held position closes at the close before the ex-date.
NSE's own adjustments are inconsistent across CA types (see spec).
Renames are bridged: TATAMOTORS->TMPV, ZOMATO->ETERNAL, SRTRANSFIN->SHRIRAMFIN.

The day loop is step(), shared verbatim with the paper trader (wheel_paper.py):
the backtest feeds it settle prices with an assumed slippage, the paper trader
feeds it live closing quotes and fills at bid/ask.

Usage
-----
    python wheel_bt.py run                                   # spec defaults
    python wheel_bt.py run --stop-mult 3 --entry-mode rank_move
    python wheel_bt.py grid --grid stops                     # stop sweep, one data load
    python wheel_bt.py grid --grid stops --from 2023-01-01   # complete-data years only
    python wheel_bt.py grid --grid rom --entry-mode all --max-new 4
    python wheel_bt.py grid --grid sizing --entry-mode all --max-new 4
    python wheel_bt.py show                                  # last runs, side by side
    python wheel_bt.py report --run latest                   # per-stock / per-year / persistence
    python wheel_bt.py split --split 2023-01-01 --entry-mode all --stop-mult 3   # IS vs OOS
    python wheel_bt.py grid --grid risk  ...     # item P: stress cap / brake on and off
    python wheel_bt.py grid --grid idio  ...     # single-stock shock scenarios (call tilt)
    python wheel_bt.py grid --grid liq   ...     # item 0: old flat cost vs spread costs + gates
    grids also: hedge, ddsize, window, ladder, hurdle, calltrend (see GRIDS)

  Grid variants are applied ON TOP of the CLI base, so set the base with
  --entry-mode / --max-new / --stop-* etc. and the grid sweeps one dimension.

Sizing
------
  fixed : 1 lot per trade (default).
  pct   : lots = floor(trade_margin_pct x capacity / margin per lot), capped at
          max_lots_per_trade and at max_vol_frac x contracts traded that day.
          A trade whose single lot exceeds the per-trade cap is skipped, not
          forced to 1 lot. util_cap and sector_cap still bound the total.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import functools
import math
import os
import sys
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

BHAV_DB = Path(os.environ.get("BHAV_DB", r"C:\Users\aniru\od\bhavcopy.duckdb"))
OC_DB = Path(os.environ.get("OC_DB", r"C:\Users\aniru\od\oc.duckdb"))
WHEEL_DB = Path(os.environ.get("WHEEL_DB", r"C:\Users\aniru\od\wheel_bt.duckdb"))

NAN = float("nan")
_RANK_KEYS, _RANK_ASC = ["_r", "symbol", "opt_type"], [False, True, True]   # stable, deterministic ranking

RENAMES = {"TATAMOTORS": "TMPV", "ZOMATO": "ETERNAL", "SRTRANSFIN": "SHRIRAMFIN", "INFRATEL": "INDUSTOWER"}
CA_TYPES = ("SPLIT", "BONUS", "DEMERGER", "RIGHTS")

# Static sector map for the sector cap (no sector data in either DB). Edit freely;
# unmapped symbols fall into OTHER and are listed at load time.
SECTOR = {
    **dict.fromkeys(["HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK", "INDUSINDBK",
                     "BANKBARODA", "CANBK", "PNB", "AUBANK", "IDFCFIRSTB", "FEDERALBNK",
                     "UNIONBANK", "YESBANK", "BANDHANBNK", "RBLBANK"], "BANK"),
    **dict.fromkeys(["BAJFINANCE", "BAJAJFINSV", "SHRIRAMFIN", "CHOLAFIN", "MUTHOOTFIN",
                     "LICHSGFIN", "SBICARD", "PFC", "RECLTD", "JIOFIN", "HDFC", "BAJAJHLDNG",
                     "M&MFIN", "PEL", "IBULHSGFIN", "IDFC", "EDELWEISS"], "NBFC"),
    **dict.fromkeys(["HDFCLIFE", "SBILIFE", "ICICIGI", "MFSL", "BSE", "ICICIPRULI", "HDFCAMC", "IEX",
                     "MCX"], "INS_CAPMKT"),
    **dict.fromkeys(["INFY", "TCS", "HCLTECH", "WIPRO", "TECHM", "LTIM"], "IT"),
    **dict.fromkeys(["MARUTI", "M&M", "BAJAJ-AUTO", "EICHERMOT", "TMPV", "HEROMOTOCO"], "AUTO"),
    **dict.fromkeys(["SUNPHARMA", "DRREDDY", "CIPLA", "APOLLOHOSP", "MAXHEALTH", "DIVISLAB"], "PHARMA_HEALTH"),
    **dict.fromkeys(["HINDUNILVR", "ITC", "NESTLEIND", "TATACONSUM", "ASIANPAINT", "TITAN",
                     "TRENT", "ETERNAL", "BRITANNIA", "ZEEL", "ITCHOTELS"], "CONSUMER"),
    **dict.fromkeys(["TATASTEEL", "JSWSTEEL", "HINDALCO", "COALINDIA", "ULTRACEMCO", "GRASIM",
                     "SHREECEM", "VEDL", "UPL"], "METALS_MATERIALS"),
    **dict.fromkeys(["RELIANCE", "ONGC", "NTPC", "POWERGRID", "BPCL", "IOC", "GAIL"], "ENERGY_UTIL"),
    **dict.fromkeys(["LT", "BEL", "ADANIENT", "ADANIPORTS", "INDIGO", "BHARTIARTL", "INDUSTOWER"],
                    "INDUS_INFRA_TEL"),
}


# ---------------------------------------------------------------------------
# parameters
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Params:
    # window
    start: date = date(2019, 10, 1)     # SPAN/bhavcopy/events complete from here (covers COVID)
    end: date = date(2026, 9, 30)
    # entry
    dte_lo: int = 30                    # calendar days
    dte_hi: int = 35
    rom_target: float = 0.02            # cap: furthest-OTM strike with ROM >= this
    delta_cap: float = 0.25             # skip if that strike's |delta| exceeds this
    sides: tuple = ("PE", "CE")
    symbols: Optional[tuple] = None     # whitelist; None = whole universe
    universe: str = "pit"               # 'pit': index members AS OF each day (index_constituents_hist,
                                        # month-end list applied from the next trading day); 'current':
                                        # today's index_constituents for every date (survivorship-biased)
    entry_mode: str = "rank_move"       # 'rank_move' (put red / call green, by |move|) | 'all'
    move_cap: Optional[float] = 0.04    # skip names that moved more than this today
    max_new_per_day: int = 2
    max_lots_per_symbol_side: int = 1
    # liquidity (plan item 0). Measured 15:40 spreads (5%+ OTM, 14-45 DTE, ~74k
    # quotes): median full spread 107% of mid below 10 contracts traded, 34% at
    # 10-24, 19% at 25-99, 9% at 100-499, 4% at 500+. The flat 10% slippage
    # undercharged thin strikes, where most of the old P&L came from.
    min_entry_volume: int = 25          # contracts traded on the entry day
    min_entry_oi: int = 50              # open interest (contracts) on the entry day
    multi_lot_min_volume: int = 100     # pct sizing: below this, 1 lot only
    cost_model: str = "spread"          # 'spread' (half-spread by volume bucket) | 'flat' (slippage)
    spread_buckets: tuple = ((10, 0.535), (25, 0.17), (100, 0.095), (500, 0.045), (None, 0.02))
    max_quote_spread: float = 0.20      # paper: skip entries with (ask-bid)/mid above this
    # entry ladder (plan item 1): up to N entries per stock-side on different days
    ladder_entries: int = 1
    ladder_min_gap_days: int = 2        # trading days between ladder entries
    # call trend filters (plan item 4; the trend-break EXIT was tested and dropped)
    call_trend_dist: Optional[float] = None   # no new calls when close > (1 + x) * 50-day average
    call_trend_ret: Optional[float] = None    # no new calls when 20-session return > x
    earn_entry_days: int = 5            # no entry if results within N trading days
    event_scope: str = "board"          # 'board' (all board meetings bar AGM/EGM) | 'results'
    pit_events: bool = True             # only events already announced on the day
    # corporate-action blackout (28 Oct fix). 'adjusting' = only events NSE adjusts F&O contracts
    # for: split, bonus, rights, demerger, and dividends >= 5% (to 28-Jun-2022) / 2% (from
    # 29-Jun-2022) of the share price. 'all' = the pre-28-Oct set (every ex-date: ordinary
    # dividends, AGM / interest record dates ...). pit_ca: an ex-date counts only from the
    # session after the board meeting that announced it (latest meeting <= 120 days before);
    # without a matched meeting, from 7 calendar days before the ex-date.
    ca_scope: str = "adjusting"
    pit_ca: bool = True
    require_event_coverage: bool = True # no entry before a symbol's event history starts
    # exits (first to fire wins, checked in this order)
    earn_exit_lead: int = 1             # close N trading days before results date
    stop_mult: Optional[float] = 1.5    # settle >= mult x entry premium
    stop_requires_trade: bool = False   # only fire stop_mult on days the contract traded
    stop_spot_cross: bool = False       # spot through strike
    stop_delta: Optional[float] = None  # |delta| above this
    profit_residual: Optional[float] = None   # close when mark <= this x entry premium
    time_exit_td: int = 5               # at <= N trading days left, close if |delta| > near_money
    near_money_delta: float = 0.15
    final_exit_td: int = 2              # at <= N trading days left, close everything
    # hurdle exits (plan item 2). remaining yield = premium still in the position /
    # current margin x 365 / calendar days left (gross of exit costs: closing now
    # or at the final exit both pay a spread).
    exit_hurdle: Optional[float] = None # hard: close whenever remaining yield < this (e.g. FD rate 0.07)
    hurdle_swap: Optional[float] = None # soft: below this, close only to make room for a waiting entry
    # portfolio
    cash: float = 25_00_000
    collateral: float = 25_00_000       # margin value of pledged securities
    util_cap: float = 0.50              # used margin <= util_cap x capacity
    sector_cap: float = 0.30            # sector margin <= sector_cap x (util_cap x capacity)
    compound: bool = True
    sizing: str = "fixed"               # 'fixed' (1 lot) | 'pct' (see module doc)
    trade_margin_pct: float = 0.05      # pct sizing: margin per trade <= this x capacity
    max_lots_per_trade: int = 20
    max_vol_frac: float = 0.20          # pct sizing: lots <= this x contracts traded on entry day
    # portfolio risk layer (plan item P). The COVID run lost all its cash in two
    # sessions with every per-trade rule intact: 19 short puts were one bet.
    stress_cap: Optional[float] = 0.15  # book scenario loss <= this x cash equity; None = off
    stress_trim_at: Optional[float] = None  # close biggest contributors while stress > this x cap
                                        # (the cap gates entries only; marks drift, up to 2.5x cap)
    rank_by: str = "rom"                # entry_mode 'all': 'rom' (premium / margin) or
                                        # 'stress' (premium / worst-scenario loss: spends the
                                        # binding budget where it buys the most premium)
    price_basis: str = "close"          # fills: 'close' = bhavcopy CLOSE (ClsPric: traded, last-30-min VWAP;
                                        # always inside the day's high-low) for entries and for exits on
                                        # days the contract traded; 'settle' = the pre-22-Oct basis. NSE's
                                        # option SETTLE is the SPAN theoretical price when the contract
                                        # did not trade late: 1-4% of liquid contracts, but 11% of the
                                        # strategy's entries, ~2x the day high (ROM / ranking pick them)
    # IV-rich entry gate (plan item 5): rich = entry premium / model price at NSE's SPAN vol
    # (Black-Scholes on spot with rate rich_rate, i.e. the forward; no dividends). 23 Oct study,
    # clean runs: rich > 1.05 kept 37-57% of premium sold, cheap / fair 0-16%. Off by default.
    min_rich: Optional[float] = None    # skip candidates with rich below this (and those with no SPAN vol)
    rich_rate: float = 0.065
    # interest on cash (plan step 5): the cash half of margin can sit in liquid funds /
    # T-bills pledged as cash-equivalent collateral. OFF by default so runs stay comparable.
    cash_yield: float = 0.0             # annual yield on net cash (cash - option liability), accrued
                                        # daily on calendar days, e.g. 0.065
    cash_yield_file: Optional[str] = None   # CSV date,rate: a dated rate table (each rate applies from
                                        # its date); overrides cash_yield, which is used before its first row
    cash_haircut: float = 0.0           # margin haircut on that cash (liquid funds ~10%): capacity
                                        # counts cash x (1 - haircut); the 50% cash rule is unchanged
    iv_source: str = "span"             # stress-test IV: 'span' = NSE SPAN file vol per contract (stable,
                                        # no root-finding; falls back to the price-implied IV, then the
                                        # position's last IV, then 40%); 'implied' = the pre-22-Oct method
                                        # (IV backed out of settle, 40% fallback; noisy on cheap options)
    stress_moves: tuple = ((-0.10, 1.5), (-0.15, 1.75), (-0.20, 2.0), (0.10, 1.0), (0.15, 1.1))
                                        # (NIFTY move, IV multiplier); stock move = beta x NIFTY move
    # single-stock shocks: (stock move, IV multiplier) hitting ONE name at a time, not
    # beta-scaled. The index set above barely charges short calls (+15% x 1.1 IV), so
    # rank_by='stress' tilted the book ~73% calls and call stops cost Rs 39L (21 Oct).
    # Book check: the worst single name's loss <= stress_idio_cap (else stress_cap) x cash.
    stress_idio: tuple = ()              # e.g. ((0.20, 1.3),)  or  ((0.20, 1.3), (-0.20, 1.3))
    stress_idio_cap: Optional[float] = None
    beta_bounds: tuple = (0.5, 2.0)     # clip rolling 250-session beta
    # market brake: OFF by default. Grid 19 Oct: with the stress cap on, the brake cut
    # P&L from Rs 13.4L to 9.0L at the same drawdown (it blocks the rich post-fall entries).
    # Typical setting when used: 25.0 / 0.30 / 0.05 (--brake).
    brake_vix_level: Optional[float] = None   # no new brake_sides entries while India VIX above this
    brake_vix_jump: Optional[float] = None    # ... or VIX more than this above its 5-session low
    brake_nifty_dd: Optional[float] = None    # ... or NIFTY more than this below its 10-session high
    brake_sides: tuple = ("PE",)
    # put regime gate (item 6, 26 Oct): no new puts while NIFTY is below its put_block_dma-day
    # average AND India VIX is below put_block_vix (None = any VIX) -- the early part of a fall,
    # before IV pays for it. Candidate study: puts there -0.14% (IS) / -1.31% (OOS) P&L/margin,
    # stops 14% / 23%, vs +1.30% / +0.72% in uptrends; after deep falls (VIX>=25) puts are best.
    put_block_dma: Optional[int] = None      # 50 or 200
    put_block_vix: Optional[float] = None
    # IV / HV gate (item 10): enter only if ATM IV (nearest-strike traded call + put, same
    # expiry, traded close) / HV20 (20-session close-to-close, CA-adjusted) <= max_iv_hv.
    # Candidate study: IV/HV <= 0.8 earned ~1.1% (IS) / 0.65% (OOS) P&L/margin vs ~0.45 / 0.15
    # above it. No ATM IV or HV -> skipped (fail closed).
    max_iv_hv: Optional[float] = None
    # strike choice (item 11) among strikes passing rom_target / delta_cap / DTE / liquidity:
    # 'delta' = lowest |delta| (furthest OTM; the default), 'rich' = highest premium / model
    # price at SPAN vol, 'rom' = highest premium / margin. Backtest only for now.
    pick_by: str = "delta"
    # richness refinements (27 Oct). Price ratio explodes in the far wings (below ~0.05 delta
    # 93% of candidates read rich, uninformative); the vol ratio does not.
    rich_measure: str = "price"         # gate/pick on 'price' = premium / model price at SPAN vol,
                                        # or 'vol' = strike IV (from the premium) / SPAN vol
    min_delta: Optional[float] = None   # qualifying strikes need |delta| >= this (delta band floor)
    rich_confirm_days: int = 0          # gate must also have passed on the previous N sessions
                                        # (same symbol / side, that day's pick); backtest only
    # notional caps (27 Oct): margin sizing leaves the underlying exposure free -- open notional
    # ran 6x cash (median), single stocks up to 6.5x, puts 1.4-3.2x. Notional = qty x strike,
    # i.e. what physical delivery would cost if a position could not be closed.
    max_symbol_notional: Optional[float] = None   # per stock, both sides: <= this x cash equity
    max_put_notional: Optional[float] = None      # all open puts: <= this x (cash equity + collateral)
    dd_halve_at: Optional[float] = None # pct sizing halves while drawdown from peak > this x cash
    # index put hedge (backtest only): long NIFTY put sized to the beta-weighted
    # short-put notional, rolled before expiry
    hedge: bool = False
    hedge_otm: float = 0.10             # strike ~ this far below NIFTY
    hedge_ratio: float = 0.5            # hedge notional / beta-weighted short-put notional
    hedge_dte_lo: int = 25
    hedge_dte_hi: int = 70
    hedge_roll_td: int = 5              # roll at <= N trading days left
    hedge_slip: float = 0.03            # half-spread on NIFTY options, each side
    # margin + costs
    elm: float = 0.035
    broker_mult: float = 1.0
    slippage: float = 0.10              # fraction of premium, each side
    untraded_slip: float = 0.10         # extra when exiting on a day the contract did not trade
    # exit liquidity (28 Oct review): the exit's half-spread uses the EXIT day's volume only
    # ('max' = the old max(exit, entry) volume). An exit decided on a day the contract cannot be
    # bought back ('defer': no trade, and in the paper trader no ask either) waits for the next
    # tradable session with its reason kept; reaching expiry it settles at intrinsic, an ITM
    # position paying delivery_cost x strike x qty (physical delivery). 'slip' = old behaviour:
    # exit anyway at the mark x (1 + half-spread + untraded_slip).
    exit_cost_vol: str = "exit"
    untraded_exit: str = "defer"
    delivery_cost: float = 0.001
    brokerage: float = 20.0             # Rs per order

    def key(self) -> str:
        d = {k: (str(v) if isinstance(v, date) else v) for k, v in asdict(self).items()}
        return hashlib.sha1(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:10]


def params_to_json(p: Params) -> str:
    return json.dumps(asdict(p), default=str)


def params_from_json(text: str) -> Params:
    d = json.loads(text)
    for k in ("start", "end"):
        if isinstance(d.get(k), str):
            d[k] = datetime.strptime(d[k], "%Y-%m-%d").date()
    for k in ("sides", "symbols", "brake_sides", "beta_bounds"):
        if isinstance(d.get(k), list):
            d[k] = tuple(d[k])
    for k in ("spread_buckets", "stress_moves", "stress_idio"):
        if isinstance(d.get(k), list):
            d[k] = tuple(tuple(x) for x in d[k])
    known = set(Params.__dataclass_fields__)
    return Params(**{k: v for k, v in d.items() if k in known})


def data_key(p: Params) -> tuple:
    """Fields that change the SQL pull; everything else is replayed on one load."""
    return (p.start, p.end, p.dte_lo, p.dte_hi, p.rom_target, p.delta_cap, p.elm, p.broker_mult,
            p.min_entry_volume, p.min_entry_oi, p.hedge, p.hedge_dte_lo, p.hedge_dte_hi, p.universe, p.price_basis,
            p.pick_by, p.min_delta)


def half_spread(p: Params, volume: float) -> float:
    """Fraction of premium paid per side for a contract trading `volume` contracts a day."""
    if p.cost_model != "spread":
        return p.slippage
    v = volume if volume == volume else 0.0
    for hi, hs in p.spread_buckets:
        if hi is None or v < hi:
            return hs
    return p.spread_buckets[-1][1]


# ---------------------------------------------------------------------------
# costs (date-aware)
# ---------------------------------------------------------------------------

def stt_sell(d: date) -> float:            # options, on premium, sell side
    if d < date(2023, 4, 1):
        return 0.0005
    if d < date(2024, 10, 1):
        return 0.000625
    return 0.001


def txn_rate(d: date) -> float:            # NSE options transaction charge, on premium
    return 0.00053 if d < date(2024, 10, 1) else 0.0003503


def order_costs(d: date, premium_value: float, side: str, brokerage: float) -> float:
    """All statutory + broker costs for one option order (Rs). side 'S' or 'B'."""
    txn = txn_rate(d) * premium_value
    sebi = premium_value * 10 / 1e7
    gst = 0.18 * (brokerage + txn + sebi)
    stt = stt_sell(d) * premium_value if side == "S" else 0.0
    stamp = 0.00003 * premium_value if side == "B" else 0.0
    return brokerage + txn + sebi + gst + stt + stamp


# ---------------------------------------------------------------------------
# option pricing for the stress test (Black-76 on the spot, r = 0)
# ---------------------------------------------------------------------------
# Scenario P&L only needs a consistent model: implied vol is backed out of
# today's mark with the same formula that reprices it, so the rate and
# forward-vs-spot conventions cancel to first order.

def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(side: str, s: float, k: float, t: float, vol: float) -> float:
    if t <= 0 or vol <= 0 or s <= 0:
        return max(k - s, 0.0) if side == "PE" else max(s - k, 0.0)
    sd = vol * math.sqrt(t)
    d1 = (math.log(s / k) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    if side == "PE":
        return k * _ncdf(-d2) - s * _ncdf(-d1)
    return s * _ncdf(d1) - k * _ncdf(d2)


@functools.lru_cache(maxsize=8)
def load_rate_table(path: str) -> tuple:
    """(dates, rates) from a CSV with columns date, rate (decimal; percents > 1 are divided by 100)."""
    df = pd.read_csv(path, comment="#")
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df.sort_values("date")
    r = df["rate"].astype(float)
    r = r.where(r <= 1.0, r / 100.0)
    return list(df["date"]), list(r)


def cash_rate(p: "Params", d: date) -> float:
    """Annual yield on cash in force on day d."""
    if p.cash_yield_file:
        v = asof(load_rate_table(str(p.cash_yield_file)), d)
        if v is not None:
            return v
    return p.cash_yield


def bs_price_r(side: str, s: float, k: float, t: float, vol: float, r: float) -> float:
    """Black-Scholes with a rate (spot S, no dividends) = Black-76 on the forward S e^rt."""
    if t <= 0 or vol <= 0 or s <= 0 or k <= 0:
        return max(k - s, 0.0) if side == "PE" else max(s - k, 0.0)
    sd = vol * math.sqrt(t)
    d1 = (math.log(s / k) + (r + 0.5 * vol * vol) * t) / sd
    d2 = d1 - sd
    dk = k * math.exp(-r * t)
    if side == "PE":
        return dk * _ncdf(-d2) - s * _ncdf(-d1)
    return s * _ncdf(d1) - dk * _ncdf(d2)


def richness(side: str, premium: float, s: float, k: float, t: float, span_vol: float, r: float = 0.065) -> float:
    """Entry premium / model price at the SPAN vol. > 1: the market pays more than NSE's vol implies.
    NaN when there is no usable SPAN vol or the model price is ~0."""
    if not (span_vol == span_vol and IV_BOUNDS[0] <= span_vol <= IV_BOUNDS[1]) or not premium > 0:
        return NAN
    m = bs_price_r(side, s, k, t, span_vol, r)
    return premium / m if m > 1e-6 else NAN


def implied_vol(side: str, price: float, s: float, k: float, t: float, fallback: float = 0.40) -> float:
    """Bisection on [1%, 500%]; fallback when the price sits at or below intrinsic."""
    intrinsic = max(k - s, 0.0) if side == "PE" else max(s - k, 0.0)
    if not (price == price) or price <= intrinsic + 1e-9 or t <= 0 or s <= 0 or k <= 0:
        return fallback
    lo, hi = 0.01, 5.0
    if bs_price(side, s, k, t, hi) < price:
        return hi
    for _ in range(32):                 # 5.0 / 2^32: far below a paisa
        mid = 0.5 * (lo + hi)
        if bs_price(side, s, k, t, mid) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


IV_BOUNDS = (0.02, 3.0)
STRESS_NONFINITE = [0]          # count of scenario losses that could not be priced (fail-closed)


def stress_iv(p: Params, side: str, mark: float, s: float, k: float, t: float,
              span_vol: float = NAN, last_iv: float = NAN) -> float:
    """IV for the stress test. 'span': SPAN vol if sane, else price-implied, else last IV, else 40%."""
    if p.iv_source != "span":
        return implied_vol(side, mark, s, k, t)
    lo, hi = IV_BOUNDS
    if span_vol == span_vol and lo <= span_vol <= hi:
        return span_vol
    fb = last_iv if (last_iv == last_iv and lo <= last_iv <= hi) else 0.40
    iv = implied_vol(side, mark, s, k, t, fallback=fb)
    return iv if lo <= iv <= hi else fb


def scenario_losses(p: Params, side: str, mark: float, s: float, k: float, t: float, qty: float,
                    beta: float, short: bool = True, shocks: Optional[tuple] = None,
                    iv: Optional[float] = None) -> list:
    """Loss (Rs, + = loss) of one position in each stress scenario.

    qty in units (shares). A short loses when the option reprices up; a long
    (the hedge) gains. Stock move = beta x NIFTY move, IV x the scenario multiplier.
    shocks: use these (move, IV mult) instead of p.stress_moves (pass beta=1 for single-stock).
    iv: the IV to shock (stress_iv); None = back it out of `mark`.

    Fail-closed: a loss that cannot be computed (NaN / inf input) is replaced by the
    whole notional (qty x strike), so it blocks entries instead of silently passing a
    `max(...) > cap` test (every comparison with NaN is False). Counted in STRESS_NONFINITE.
    """
    if iv is None or not (iv == iv and iv > 0):
        iv = implied_vol(side, mark, s, k, t)
    out = []
    for move, ivm in (p.stress_moves if shocks is None else shocks):
        try:
            s2 = s * max(1.0 + beta * move, 0.01)
            px = bs_price(side, s2, k, t, iv * ivm)
            pnl_long = (px - mark) * qty
            loss = pnl_long if short else -pnl_long
        except (ValueError, ZeroDivisionError, OverflowError):
            loss = NAN
        if not math.isfinite(loss):
            STRESS_NONFINITE[0] += 1
            loss = abs(qty) * (k if k == k and k > 0 else (s if s == s and s > 0 else 1e9))
            loss = loss if short else -loss
        out.append(loss)
    return out


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------

@dataclass
class Data:
    calendar: list                       # trading days, sorted
    cands: pd.DataFrame                  # one row per (day, symbol, side) candidate
    paths: dict                          # contract key -> {date: row tuple}
    results: dict                        # canon symbol -> sorted list of results dates
    ca: dict                             # canon symbol -> sorted list of CA ex-dates
    sector: dict
    board: dict = field(default_factory=dict)     # canon symbol -> all board-meeting dates (bar AGM/EGM)
    known: dict = field(default_factory=dict)     # (canon, event_date) -> date first announced
    coverage: dict = field(default_factory=dict)  # canon symbol -> first results date on record
    market: dict = field(default_factory=dict)    # see load_market()
    hedge: dict = field(default_factory=dict)     # see load_hedge_chain()
    ca_adj: Optional[dict] = None                 # canon symbol -> contract-adjusting CA ex-dates
    ca_known: Optional[dict] = None               # (canon, ex_date) -> first date it was knowable;
                                                  # None = every listed ex-date is already known (paper)


def _alias_sql() -> str:
    return "(VALUES " + ", ".join(f"('{a}', '{b}')" for a, b in RENAMES.items()) + ") AS a(raw, canon)"


# One candidate per (day, symbol, side): the lowest |delta| strike with ROM >= target.
# Ties (deep-OTM strikes of low-priced stocks carry SPAN delta exactly 0.0, ~0.2% of picks)
# break on the furthest-OTM strike, then the nearest expiry, then the raw symbol, so a rerun
# picks the same contract. (Until 25 Oct 2026 this was arg_min per column: with a tie the
# database could return a different row -- and in principle mix fields of tied rows.)
PICK_SQL = """pk AS (SELECT trade_date, symbol, opt_type, raw, expiry, strike, close AS premium, lot, margin, rom,
                          abs(delta) AS delta, und, oi, volume, svol
                   FROM r WHERE rom >= ?
                   QUALIFY row_number() OVER (PARTITION BY trade_date, symbol, opt_type
                       ORDER BY abs(delta), CASE WHEN opt_type = 'PE' THEN strike ELSE -strike END,
                                expiry, raw) = 1)"""


# every qualifying strike (pick_by != 'delta'; pick_strikes chooses in python)
ALL_SQL = """pk AS (SELECT trade_date, symbol, opt_type, raw, expiry, strike, close AS premium, lot, margin, rom,
                          abs(delta) AS delta, und, oi, volume, svol
                   FROM r WHERE rom >= ?)"""


def implied_vol_r(side: str, price: float, s: float, k: float, t: float, r: float) -> float:
    """Implied vol with a rate (same model as richness()); NaN when not solvable."""
    if not (price == price and price > 0 and s > 0 and k > 0 and t > 0):
        return NAN
    if price <= bs_price_r(side, s, k, t, 0.005, r) + 1e-9:
        return NAN
    lo, hi = 0.005, 5.0
    if bs_price_r(side, s, k, t, hi, r) < price:
        return NAN
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        if bs_price_r(side, s, k, t, mid, r) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def vol_ratio(side: str, premium: float, s: float, k: float, t: float, span_vol: float, r: float = 0.065) -> float:
    """Strike IV backed out of the premium / SPAN vol. NaN without a usable SPAN vol or IV."""
    if not (span_vol == span_vol and IV_BOUNDS[0] <= span_vol <= IV_BOUNDS[1]):
        return NAN
    iv = implied_vol_r(side, premium, s, k, t, r)
    return iv / span_vol if iv == iv else NAN


def vol_ratio_col(rows: pd.DataFrame, r: float) -> list:
    return [vol_ratio(s_, float(pr), float(u), float(k), max((_d(e) - _d(d)).days, 1) / 365.0, float(v), r)
            for s_, pr, u, k, e, d, v in zip(rows["opt_type"], rows["premium"], rows["und"], rows["strike"],
                                               rows["expiry"], rows["trade_date"], rows["svol"])]


def price_ratio_col(rows: pd.DataFrame, r: float) -> list:
    return [richness(s_, float(pr), float(u), float(k), max((_d(e) - _d(d)).days, 1) / 365.0, float(v), r)
            for s_, pr, u, k, e, d, v in zip(rows["opt_type"], rows["premium"], rows["und"], rows["strike"],
                                               rows["expiry"], rows["trade_date"], rows["svol"])]


def add_rich_history(cands: pd.DataFrame, cal: list, p: "Params", n: int = 2) -> pd.DataFrame:
    """rich_px / rich_vol per candidate, plus the same symbol-side's values on each of the
    previous n sessions (NaN when it had no candidate that session) for rich_confirm_days."""
    if not len(cands):
        return cands
    cands = cands.copy()
    cands["rich_px"] = price_ratio_col(cands, p.rich_rate)
    cands["rich_vol"] = vol_ratio_col(cands, p.rich_rate)
    idx = {dd: i for i, dd in enumerate(cal)}
    cands["_ti"] = [idx.get(_d(x), -10**6) for x in cands["trade_date"]]
    base = cands[["symbol", "opt_type", "_ti", "rich_px", "rich_vol"]]
    for k in range(1, n + 1):
        prev = base.assign(_ti=base["_ti"] + k).rename(columns={"rich_px": f"rich_px_p{k}", "rich_vol": f"rich_vol_p{k}"})
        cands = cands.merge(prev, on=["symbol", "opt_type", "_ti"], how="left")
    return cands.drop(columns=["_ti"])


def pick_strikes(rows: pd.DataFrame, p: "Params") -> pd.DataFrame:
    """One strike per (day, symbol, side): highest 'rich' or 'rom'; NaN scores rank last.
    Ties as PICK_SQL: lowest |delta|, furthest OTM, nearest expiry, raw."""
    if not len(rows):
        return rows
    if p.pick_by == "rich":
        score = [richness(s_, float(pr), float(u), float(k), max((e - d).days, 1) / 365.0, float(v), p.rich_rate)
                 for s_, pr, u, k, e, d, v in zip(rows["opt_type"], rows["premium"], rows["und"], rows["strike"],
                                                    rows["expiry"], rows["trade_date"], rows["svol"])]
    elif p.pick_by == "volrich":
        score = vol_ratio_col(rows, p.rich_rate)
    elif p.pick_by == "rom":
        score = rows["rom"].astype(float).tolist()
    else:
        raise ValueError(f"pick_by {p.pick_by!r}")
    x = rows.assign(_s=pd.Series(score, index=rows.index).fillna(-1e18),
                    _otm=[k if s_ == "PE" else -k for s_, k in zip(rows["opt_type"], rows["strike"].astype(float))])
    x = x.sort_values(["trade_date", "symbol", "opt_type", "_s", "delta", "_otm", "expiry", "raw"],
                      ascending=[True, True, True, False, True, True, True, True], kind="mergesort")
    return x.drop_duplicates(["trade_date", "symbol", "opt_type"]).drop(columns=["_s", "_otm"]).reset_index(drop=True)


BEST_TAG = """
    SELECT trade_date, arg_max(tag, CASE WHEN tag = 's' THEN 9 ELSE TRY_CAST(right(tag, 1) AS INT) END) AS tag
    FROM bc.span_ingest_log GROUP BY 1
"""


def connect(oc_db: Path = OC_DB, bhav_db: Path = BHAV_DB):
    import duckdb
    con = duckdb.connect(str(oc_db), read_only=True)
    con.execute(f"ATTACH '{bhav_db}' AS bc (READ_ONLY)")
    return con


def load_events(con, start: date, end: date) -> tuple[dict, dict]:
    """Results dates and corporate-action ex-dates per CANONICAL symbol, sorted."""
    ev = load_events_full(con, start, end)
    return ev["results"], ev["ca"]


def load_events_full(con, start: date, end: date) -> dict:
    """Everything the event gates need, per CANONICAL symbol.

    results  : results-meeting dates
    board    : all board-meeting dates except AGM/EGM-only rows (superset of results)
    known    : (symbol, event_date) -> first announcement date, where the source has it
    coverage : first results date on record (whole table, not the window)
    ca       : corporate-action ex-dates
    """
    cols = {r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_catalog = 'bc' "
        "AND table_name = 'corporate_events'").fetchall()}
    agm = ("bool_and(coalesce(event_type, '') = 'AGM_EGM')" if "event_type" in cols else "false")
    ann = ("min(announced_at)::DATE" if "announced_at" in cols else "NULL::DATE")
    rows = con.execute(f"""
        SELECT symbol, event_date, bool_or(is_results), {agm}, {ann}
        FROM bc.corporate_events WHERE event_date BETWEEN ? AND ? GROUP BY 1, 2""", [start, end]).fetchall()
    results: dict = {}
    board: dict = {}
    known: dict = {}
    for sym, d, is_res, agm_only, ann in rows:
        c, d = RENAMES.get(sym, sym), _d(d)
        if is_res:
            results.setdefault(c, []).append(d)
        if is_res or not agm_only:
            board.setdefault(c, []).append(d)
        if ann is not None:
            k = _d(ann)
            known[(c, d)] = min(known.get((c, d), k), k)
    coverage: dict = {}
    for sym, d in con.execute("SELECT symbol, min(event_date) FROM bc.corporate_events "
                              "WHERE is_results GROUP BY 1").fetchall():
        c, d = RENAMES.get(sym, sym), _d(d)
        coverage[c] = min(coverage.get(c, d), d)
    ca: dict = {}
    for sym, d in con.execute(f"""
            SELECT symbol, ex_date FROM bc.corporate_actions
            WHERE ca_type IN {CA_TYPES} AND ex_date BETWEEN ? AND ?
            UNION SELECT symbol, trade_date FROM bc.cash_returns_adj
            WHERE is_ca AND trade_date BETWEEN ? AND ?""", [start, end, start, end]).fetchall():
        ca.setdefault(RENAMES.get(sym, sym), []).append(_d(d))
    # contract-adjusting subset + knowability (28 Oct; hardened after review). Columns are
    # inspected first: a missing div_amount / ca_type / meeting table never widens the blackout
    # or switches knowability off -- missing amounts count as adjusting (blocks, conservative),
    # missing meetings fall back to "known 7 days before the ex-date".
    def _cols(table: str) -> set:
        return {r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_catalog = 'bc' AND table_name = ?",
            [table]).fetchall()}
    ca_cols, cr_cols, ce_cols, su_cols = (_cols("corporate_actions"), _cols("cash_returns_adj"),
                                          _cols("corporate_events"), _cols("span_und"))
    parts, prm = [], []
    if {"symbol", "ex_date"} <= ca_cols:
        t_ = "upper(coalesce(ca_type, ''))" if "ca_type" in ca_cols else "''"
        a_ = "div_amount" if "div_amount" in ca_cols else "NULL::DOUBLE"
        parts.append(f"SELECT symbol, ex_date, {t_} AS t, {a_} AS div_amount FROM bc.corporate_actions "
                     f"WHERE ex_date BETWEEN ? AND ?")
        prm += [start, end]
    if {"symbol", "trade_date", "is_ca"} <= cr_cols:
        t_ = "upper(coalesce(ca_type, 'OTHER'))" if "ca_type" in cr_cols else "'OTHER'"
        parts.append(f"SELECT symbol, trade_date, {t_}, NULL::DOUBLE FROM bc.cash_returns_adj "
                     f"WHERE is_ca AND trade_date BETWEEN ? AND ?")
        prm += [start, end]
    missing = [n for n, ok in (("corporate_actions.ca_type", "ca_type" in ca_cols),
                               ("corporate_actions.div_amount", "div_amount" in ca_cols),
                               ("corporate_events", {"symbol", "event_date"} <= ce_cols),
                               ("span_und", {"symbol", "trade_date", "price"} <= su_cols)) if not ok]
    if missing:
        print(f"  note: CA classification without {missing}: unknown types/amounts count as adjusting, "
              f"unmatched announcements as known 7 days before the ex-date")
    meet_sql = ("(SELECT max(m.event_date) FROM m WHERE m.symbol = ev.symbol AND m.event_date <= ev.ex_date "
                "AND m.event_date >= ev.ex_date - 120)" if {"symbol", "event_date"} <= ce_cols else "NULL::DATE")
    m_cte = ("m AS (SELECT DISTINCT symbol, event_date FROM bc.corporate_events)," if {"symbol", "event_date"} <= ce_cols
             else "")
    px_join = ("""ASOF LEFT JOIN (SELECT symbol, trade_date, avg(price) AS px FROM bc.span_und WHERE price > 0
                  GROUP BY 1, 2) u ON u.symbol = j.symbol AND coalesce(j.meet, j.ex_date - 7) >= u.trade_date"""
               if {"symbol", "trade_date", "price"} <= su_cols else "")
    px_col = ("CASE WHEN coalesce(j.meet, j.ex_date - 7) - u.trade_date <= 30 THEN u.px END"
              if px_join else "NULL::DOUBLE")
    rows = con.execute(f"""
        WITH ev AS ({" UNION ALL ".join(parts)}),
        {m_cte}
        j AS (SELECT ev.*, {meet_sql} AS meet FROM ev)
        SELECT j.symbol, j.ex_date, j.t, j.div_amount, j.meet, {px_col} FROM j {px_join}
    """, prm).fetchall() if parts else []
    ca_adj: dict = {}
    ca_known: dict = {}
    info: dict = {}                              # (canon, ex) -> [types, amounts, price]
    for sym, ex, t, amt, meet, px in rows:
        c, ex = RENAMES.get(sym, sym), _d(ex)
        kn = (_d(meet) + timedelta(days=1)) if meet is not None else ex - timedelta(days=7)
        ca_known[(c, ex)] = min(ca_known.get((c, ex), kn), kn)
        it = info.setdefault((c, ex), [set(), [], None])
        it[0].add(t or "")
        if amt is not None and amt == amt:
            it[1].append(float(amt))
        if px and px > 0:
            it[2] = float(px)
    for (c, ex), (types, amts, px) in info.items():
        if any(any(x in t for x in ("SPLIT", "BONUS", "RIGHTS", "DEMERGER")) or not t for t in types):
            adjusting = True                     # price-adjusting, or type unknown (conservative)
        elif any("DIVIDEND" in t for t in types):
            if not amts or not px:
                adjusting = True                 # amount or price unknown: block (conservative)
            else:
                adjusting = max(amts) / px >= (0.05 if ex < date(2022, 6, 29) else 0.02)
        else:
            adjusting = False                    # AGM / EGM / interest / buyback record dates
        if adjusting:
            ca_adj.setdefault(c, []).append(ex)
    for dct in (results, board, ca, ca_adj):
        for k in dct:
            dct[k] = sorted(set(dct[k]))
    return dict(results=results, board=board, known=known, coverage=coverage, ca=ca,
                ca_adj=ca_adj, ca_known=ca_known)


def _has_table(con, catalog: str, table: str) -> bool:
    return bool(con.execute("SELECT 1 FROM information_schema.tables WHERE table_catalog = ? AND table_name = ?",
                            [catalog, table]).fetchone())


def _has_main_table(con, table: str) -> bool:
    return _has_table(con, con.execute("SELECT current_database()").fetchone()[0], table)


def universe_ctes(con, universe: str = "pit") -> tuple[str, str]:
    """SQL for the tradeable universe: (CTEs defining `u(raw, canon)` and `mem(raw, f, t)`,
    and a predicate on alias `o` (o.symbol, o.trade_date) that keeps only index members).

    'pit' -- a symbol is a member on day d when the latest month-end list dated BEFORE d
    contains it (lists are published at the month end, so they apply from the next
    trading day: no look-ahead). Symbols are the names in use at the time, so ZOMATO in
    Mar 2025 matches the bhavcopy's ZOMATO; RENAMES maps them to today's name.
    Falls back to 'current' when index_constituents_hist does not exist.
    """
    al = _alias_sql()
    if universe == "pit" and _has_main_table(con, "index_constituents_hist"):
        ctes = f"""
            lists AS (SELECT index_name, as_of, lead(as_of) OVER (PARTITION BY index_name ORDER BY as_of) AS nxt
                      FROM (SELECT DISTINCT index_name, as_of FROM index_constituents_hist)),
            mem AS (SELECT DISTINCT h.symbol AS raw, l.as_of AS f, coalesce(l.nxt, DATE '9999-12-31') AS t
                    FROM index_constituents_hist h JOIN lists l USING (index_name, as_of)),
            u AS (SELECT DISTINCT m.raw, coalesce(a.canon, m.raw) AS canon
                  FROM mem m LEFT JOIN {al} ON a.raw = m.raw),"""
        pred = ("EXISTS (SELECT 1 FROM mem WHERE mem.raw = o.symbol "
                "AND o.trade_date > mem.f AND o.trade_date <= mem.t)")
        return ctes, pred
    if universe == "pit":
        print("  note: no index_constituents_hist table -> today's index_constituents (survivorship-biased)")
    ctes = f"""
            mem AS (SELECT NULL::VARCHAR AS raw, NULL::DATE AS f, NULL::DATE AS t WHERE false),
            u AS (SELECT DISTINCT symbol AS raw, symbol AS canon FROM index_constituents
                  UNION ALL SELECT raw, canon FROM {al}),"""
    return ctes, "true"


def universe_symbols_sql(con) -> str:
    """Every canonical symbol that is or ever was in the universe (for betas / trends)."""
    sql = "SELECT symbol FROM index_constituents"
    if _has_main_table(con, "index_constituents_hist"):
        sql += (f" UNION SELECT coalesce(a.canon, h.symbol) FROM index_constituents_hist h "
                f"LEFT JOIN {_alias_sql()} ON a.raw = h.symbol")
    return sql


def load_market(con, start: date, end: date) -> dict:
    """Market series for the risk layer and the call trend filter.

    daily : date -> {vix, vix_jump (vs 5-session low), nifty_dd (vs 10-session high)}
    beta  : canon symbol -> (dates, values): rolling 250-session OLS beta of the
            CA-adjusted stock return on NIFTY 50 (>= 60 obs, so
            betas exist from Jan 2020, before the COVID crash)
    trend : canon symbol -> (dates, [(close / 50-day average - 1, 20-session return)])

    Everything is point-in-time (windows end on the day). Lookups go through
    asof() so a caller running before that day's data is loaded (the paper
    trader at 17:45) gets the latest earlier value. Missing tables -> {}.
    """
    out: dict = {"daily": {}, "beta": {}, "trend": {}, "hv20": {}}
    if not _has_table(con, "bc", "index_eod"):
        return out
    lo = start - timedelta(days=420)
    vix, nifty = {}, {}
    for d, name, close in con.execute("""
            SELECT trade_date, index_name, close FROM bc.index_eod
            WHERE index_name IN ('India VIX', 'Nifty 50') AND trade_date BETWEEN ? AND ? AND close > 0
            ORDER BY 1""", [lo, end]).fetchall():
        (vix if name == "India VIX" else nifty)[_d(d)] = float(close)
    vd, nd = sorted(vix), sorted(nifty)
    for i, d in enumerate(vd):
        lo5 = min(vix[x] for x in vd[max(0, i - 4):i + 1])
        out["daily"].setdefault(d, {})["vix"] = vix[d]
        out["daily"][d]["vix_jump"] = vix[d] / lo5 - 1 if lo5 > 0 else 0.0
    csum = [0.0]
    for d in nd:
        csum.append(csum[-1] + nifty[d])
    for i, d in enumerate(nd):
        hi10 = max(nifty[x] for x in nd[max(0, i - 9):i + 1])
        out["daily"].setdefault(d, {})["nifty_dd"] = nifty[d] / hi10 - 1
        out["daily"][d]["nifty"] = nifty[d]
        for w in (50, 200):                                   # close vs w-session average (full window only)
            if i + 1 >= w:
                out["daily"][d][f"nifty_vs{w}"] = nifty[d] / ((csum[i + 1] - csum[i + 1 - w]) / w) - 1
    if not _has_table(con, "bc", "cash_returns_adj"):
        return out
    rows = con.execute(f"""
        WITH al AS (SELECT * FROM {_alias_sql()}),
        r AS (SELECT coalesce(al.canon, c.symbol) AS sym, c.trade_date AS d, coalesce(c.log_ret, 0) AS lr
              FROM bc.cash_returns_adj c LEFT JOIN al ON al.raw = c.symbol
              WHERE c.trade_date BETWEEN ? AND ?
                AND coalesce(al.canon, c.symbol) IN ({universe_symbols_sql(con)})),
        n AS (SELECT trade_date AS d, ln(close / lag(close) OVER (ORDER BY trade_date)) AS nr
              FROM bc.index_eod WHERE index_name = 'Nifty 50' AND close > 0 AND trade_date BETWEEN ? AND ?),
        j AS (SELECT r.sym, r.d, r.lr, n.nr FROM r JOIN n USING (d)),
        b AS (SELECT sym, d, lr, regr_slope(lr, nr) OVER w AS beta, count(nr) OVER w AS nb,
                     sum(lr) OVER (PARTITION BY sym ORDER BY d) AS lp
              FROM j WINDOW w AS (PARTITION BY sym ORDER BY d ROWS BETWEEN 249 PRECEDING AND CURRENT ROW)),
        t AS (SELECT sym, d, beta, nb,
                     exp(lp) / avg(exp(lp)) OVER (PARTITION BY sym ORDER BY d ROWS BETWEEN 49 PRECEDING AND CURRENT ROW)
                         - 1 AS dist50,
                     count(*) OVER (PARTITION BY sym ORDER BY d ROWS BETWEEN 49 PRECEDING AND CURRENT ROW) AS n50,
                     exp(lp - lag(lp, 20) OVER (PARTITION BY sym ORDER BY d)) - 1 AS r20,
                     stddev_samp(lr) OVER w20 * sqrt(252) AS hv20, count(lr) OVER w20 AS n20
              FROM b
              WINDOW w20 AS (PARTITION BY sym ORDER BY d ROWS BETWEEN 19 PRECEDING AND CURRENT ROW))
        SELECT sym, d, CASE WHEN nb >= 60 THEN beta END, CASE WHEN n50 = 50 THEN dist50 END, r20,
               CASE WHEN n20 >= 15 THEN hv20 END
        FROM t WHERE d >= ? ORDER BY sym, d""", [lo, end, lo, end, start - timedelta(days=10)]).fetchall()
    for sym, d, beta, dist50, r20, hv20 in rows:
        d = _d(d)
        if hv20 is not None:
            hd = out["hv20"].setdefault(sym, ([], []))
            hd[0].append(d)
            hd[1].append(float(hv20))
        if beta is not None:
            bd = out["beta"].setdefault(sym, ([], []))
            bd[0].append(d)
            bd[1].append(float(beta))
        td = out["trend"].setdefault(sym, ([], []))
        td[0].append(d)
        td[1].append((_f(dist50), _f(r20)))
    return out


def asof(series: Optional[tuple], d: date, default=None):
    """Latest value on or before d from a (dates, values) pair."""
    if not series:
        return default
    i = bisect_right(series[0], d)
    return series[1][i - 1] if i else default


def load_hedge_chain(con, start: date, end: date, p: Params) -> dict:
    """NIFTY puts for the index hedge: exchange (SPAN) price every day.

    px  : (expiry, strike) -> {date: price}
    und : date -> NIFTY (SPAN underlying price)
    Strikes 75-97% of spot, expiries up to hedge_dte_hi days out (so held
    contracts keep a price down to expiry).
    """
    out: dict = {"px": {}, "und": {}}
    rows = con.execute(f"""
        WITH bt AS ({BEST_TAG})
        SELECT s.trade_date, s.expiry, s.strike, s.price, su.price
        FROM bc.span_contract s
        JOIN bt ON bt.trade_date = s.trade_date AND bt.tag = s.tag
        JOIN bc.span_und su ON su.trade_date = s.trade_date AND su.tag = s.tag AND su.symbol = 'NIFTY'
        WHERE s.symbol = 'NIFTY' AND s.instr = 'PE' AND s.trade_date BETWEEN ? AND ?
          AND s.expiry >= s.trade_date AND s.expiry - s.trade_date <= ?
          AND s.strike BETWEEN 0.75 * su.price AND 0.97 * su.price AND s.price > 0""",
        [start, end, p.hedge_dte_hi]).fetchall()
    for d, e, k, px, und in rows:
        d, e = _d(d), _d(e)
        out["px"].setdefault((e, float(k)), {})[d] = float(px)
        out["und"][d] = float(und)
    print(f"  hedge chain: {len(rows):,} NIFTY put rows, {len(out['px']):,} contracts")
    return out


def load_atm_iv(con, cands: pd.DataFrame) -> pd.Series:
    """ATM IV for each candidate row: mean implied vol (traded close) of the strike nearest the
    underlying, call and put, same symbol / expiry / day. NaN when neither side traded."""
    if not len(cands):
        return pd.Series(dtype=float)
    keys = cands[["trade_date", "raw", "expiry", "und"]].drop_duplicates(["trade_date", "raw", "expiry"])
    con.register("_ak", keys)
    a = con.execute("""
        SELECT k.trade_date, k.raw, k.expiry, k.und, o.opt_type, o.strike, o.close
        FROM _ak k JOIN bc.options_eod o ON o.trade_date = k.trade_date AND o.symbol = k.raw AND o.expiry = k.expiry
        WHERE o.close > 0 AND o.volume > 0
        QUALIFY row_number() OVER (PARTITION BY k.trade_date, k.raw, k.expiry, o.opt_type
                                   ORDER BY abs(o.strike / k.und - 1), o.strike) = 1""").fetchall()
    con.unregister("_ak")
    acc: dict = {}
    for d, raw, e, und, side, k, px in a:
        d, e = _d(d), _d(e)
        iv = implied_vol(side, float(px), float(und), float(k), max((e - d).days, 1) / 365.0, fallback=NAN)
        if iv == iv and IV_BOUNDS[0] < iv < 5.0 - 1e-6:
            acc.setdefault((d, raw, e), []).append(iv)
    m = {k: sum(v) / len(v) for k, v in acc.items()}
    return pd.Series([m.get((d, r, e), NAN) for d, r, e in zip(cands["trade_date"], cands["raw"], cands["expiry"])],
                     index=cands.index, dtype=float)


def load_data(p: Params, con=None) -> Data:
    own = con is None
    con = con or connect()
    try:
        cal = [_d(r[0]) for r in con.execute(
            "SELECT DISTINCT trade_date FROM bc.span_ingest_log WHERE trade_date BETWEEN ? AND ? ORDER BY 1",
            [p.start, p.end + timedelta(days=75)]).fetchall()]
        # the step's calendar must run past every open expiry (simulate still stops at p.end):
        # sessions on record after `end`, then weekdays (holidays unknown) up to end + 75 days
        x = (cal[-1] if cal else p.end) + timedelta(days=1)
        while x <= p.end + timedelta(days=75):
            if x.weekday() < 5:
                cal.append(x)
            x += timedelta(days=1)

        u_ctes, member = universe_ctes(con, p.universe)
        px_sql = "o.close" if p.price_basis == "close" else "coalesce(nullif(o.settle, 0), o.close)"
        cands = con.execute(f"""
            WITH {u_ctes}
            bt AS ({BEST_TAG}),
            o AS (SELECT o.trade_date, u.canon AS symbol, o.symbol AS raw, o.expiry, o.opt_type,
                         o.strike, {px_sql} AS close, o.volume, o.oi, o.lot
                  FROM bc.options_eod o JOIN u ON o.symbol = u.raw
                  WHERE o.trade_date BETWEEN ? AND ? AND {member}
                    AND o.expiry - o.trade_date BETWEEN ? AND ?
                    AND o.volume >= ? AND {px_sql} > 0 AND o.lot > 0
                    AND coalesce(o.oi, 0) / o.lot >= ?),          -- bhavcopy OI is in shares
            j AS (SELECT o.*, s.short_scan, s.delta, s.vol AS svol, su.price AS und
                  FROM o JOIN bt ON bt.trade_date = o.trade_date
                  JOIN bc.span_contract s ON s.trade_date = o.trade_date AND s.tag = bt.tag
                       AND s.symbol = o.raw AND s.instr = o.opt_type AND s.expiry = o.expiry AND s.strike = o.strike
                  JOIN bc.span_und su ON su.trade_date = o.trade_date AND su.tag = bt.tag AND su.symbol = o.raw
                  WHERE (o.opt_type = 'PE' AND o.strike < su.price) OR (o.opt_type = 'CE' AND o.strike > su.price)),
            m AS (SELECT *, short_scan * lot * ? + ? * und * lot AS margin FROM j),
            r AS (SELECT *, close * lot / margin AS rom FROM m WHERE margin > 0 AND abs(delta) >= {float(p.min_delta or 0.0)!r}),
            {PICK_SQL if p.pick_by == "delta" else ALL_SQL}
            SELECT pk.*, exp(ra.log_ret) - 1 AS move
            FROM pk LEFT JOIN bc.cash_returns_adj ra ON ra.symbol = pk.raw AND ra.trade_date = pk.trade_date
            WHERE pk.delta <= ?
            ORDER BY pk.trade_date, pk.symbol, pk.opt_type
        """, [p.start, p.end, p.dte_lo, p.dte_hi, max(p.min_entry_volume, 1), max(p.min_entry_oi, 0),
              p.broker_mult, p.elm, p.rom_target, p.delta_cap]).df()
        for c in ("trade_date", "expiry"):
            cands[c] = pd.to_datetime(cands[c]).dt.date
        if p.pick_by != "delta":
            n_all = len(cands)
            cands = pick_strikes(cands, p)
            print(f"  strike pick by {p.pick_by}: {n_all:,} qualifying strikes -> {len(cands):,} candidates")
        cands = add_rich_history(cands, cal, p)
        cands["atm_iv"] = load_atm_iv(con, cands)

        keys = cands[["raw", "opt_type", "expiry", "strike"]].drop_duplicates()
        first = cands.groupby(["raw", "opt_type", "expiry", "strike"], as_index=False)["trade_date"].min()
        keys = keys.merge(first, on=["raw", "opt_type", "expiry", "strike"])
        con.register("_k", keys)
        pth = con.execute(f"""
            WITH bt AS ({BEST_TAG})
            SELECT s.symbol AS raw, s.instr AS opt_type, s.expiry, s.strike, s.trade_date,
                   o.close, coalesce(o.volume, 0) AS volume, s.price AS sprice, s.delta AS sdelta,
                   s.short_scan, su.price AS und, s.vol AS svol
            FROM _k k
            JOIN bc.span_contract s ON s.symbol = k.raw AND s.instr = k.opt_type AND s.expiry = k.expiry
                 AND s.strike = k.strike AND s.trade_date >= k.trade_date AND s.trade_date <= k.expiry
            JOIN bt ON bt.trade_date = s.trade_date AND bt.tag = s.tag
            LEFT JOIN bc.span_und su ON su.trade_date = s.trade_date AND su.tag = s.tag AND su.symbol = s.symbol
            LEFT JOIN bc.options_eod o ON o.trade_date = s.trade_date AND o.symbol = s.symbol
                 AND o.expiry = s.expiry AND o.strike = s.strike AND o.opt_type = s.instr
        """).df()
        # days the contract TRADED but has no SPAN row (rare file gaps, e.g. TATAMOTORS 12-Jun-2020):
        # keep a row with the traded close so the position is marked and exits at that price
        gap = con.execute(f"""
            SELECT o.symbol AS raw, o.opt_type, o.expiry, o.strike, o.trade_date, o.close, o.volume
            FROM _k k JOIN bc.options_eod o ON o.symbol = k.raw AND o.opt_type = k.opt_type
                 AND o.expiry = k.expiry AND o.strike = k.strike
                 AND o.trade_date >= k.trade_date AND o.trade_date < k.expiry
            WHERE o.volume > 0 AND o.close > 0""").fetchall()       # expiry day: SPAN drops expiring series
        con.unregister("_k")
        paths: dict = {}
        for r in pth.itertuples(index=False):
            k = (r.raw, r.opt_type, _d(r.expiry), float(r.strike))
            paths.setdefault(k, {})[_d(r.trade_date)] = (
                _f(r.close), float(r.volume), _f(r.sprice), _f(r.sdelta), _f(r.short_scan), _f(r.und), NAN,
                _f(r.svol))
        n_gap = 0
        for raw, ot, e, k_, td, cl, vo in gap:
            pk = paths.get((raw, ot, _d(e), float(k_)))
            if pk is not None and _d(td) not in pk:
                pk[_d(td)] = (float(cl), float(vo), NAN, NAN, NAN, NAN, NAN, NAN)
                n_gap += 1
        if n_gap:
            print(f"  {n_gap} path day(s) with a trade but no SPAN row: marked at the traded close")

        ev = load_events_full(con, p.start, p.end + timedelta(days=60))
        mkt = load_market(con, p.start, p.end)
        hedge = load_hedge_chain(con, p.start, p.end, p) if p.hedge else {}
    finally:
        if own:
            con.close()

    unmapped = sorted(set(cands["symbol"]) - set(SECTOR))
    if unmapped:
        print(f"  note: no sector for {unmapped} -> OTHER")
    print(f"  universe: {p.universe}, {cands['symbol'].nunique()} symbols with candidates")
    print(f"  loaded: {len(cal)} days, {len(cands):,} candidates, {len(paths):,} contract paths, "
          f"{sum(map(len, paths.values())):,} path rows; market: {len(mkt['daily'])} days, "
          f"{len(mkt['beta'])} betas")
    return Data(cal, cands, paths, ev["results"], ev["ca"], dict(SECTOR),
                ev["board"], ev["known"], ev["coverage"], mkt, hedge, ev["ca_adj"], ev["ca_known"])


def _d(v) -> date:
    return v if isinstance(v, date) and not isinstance(v, datetime) else pd.Timestamp(v).date()


def _f(v) -> float:
    try:
        x = float(v)
        return x if x == x else float("nan")
    except (TypeError, ValueError):
        return float("nan")


# ---------------------------------------------------------------------------
# simulation
# ---------------------------------------------------------------------------

@dataclass
class Pos:
    pid: int
    symbol: str
    raw: str
    side: str
    expiry: date
    strike: float
    lot: int                 # quantity in UNITS (lot size x lots)
    entry_date: date
    entry_px: float          # settle price at entry (before slippage)
    credit: float            # per share, net of slippage
    entry_cost: float        # Rs
    margin: float
    sector: str
    mark: float = float("nan")
    delta: float = float("nan")
    und: float = float("nan")
    lots: int = 1
    entry_vol: float = 0.0   # contracts traded on the entry day (liquidity class for exit costs)
    rich: float = float("nan")   # entry premium / model price at SPAN vol (see richness)
    iv_hv: float = float("nan")  # ATM IV / HV20 at entry
    svol: float = float("nan")   # NSE SPAN implied vol for this contract, latest day it was published
    iv: float = float("nan")     # last IV the stress test used (fallback when today's is unusable)
    exit_pending: str = ""       # exit decided on an untradable day; carried to the next tradable one

    @property
    def key(self) -> tuple:
        return (self.raw, self.side, self.expiry, float(self.strike))


@dataclass
class Hedge:
    """Long NIFTY put (backtest only). qty in index units, fractional allowed."""
    hid: int
    expiry: date
    strike: float
    qty: float
    entry_date: date
    paid: float              # per unit, incl. half-spread
    entry_cost: float        # Rs
    mark: float


@dataclass
class Result:
    params: Params
    trades: pd.DataFrame
    equity: pd.DataFrame
    metrics: dict


def _next_on_or_after(dates: list, d: date) -> Optional[date]:
    """First event dated d or later (an event today must still be acted on)."""
    i = bisect_left(dates, d)
    return dates[i] if i < len(dates) else None


@dataclass
class State:
    """Everything that carries from one session to the next. Persisted by the paper trader."""
    cash: float
    book: list = field(default_factory=list)      # open Pos
    pid: int = 0
    peak: float = 0.0                             # highest recorded equity (drawdown sizing)
    hedges: list = field(default_factory=list)    # open Hedge (backtest only)
    last_day: Optional[date] = None               # previous session (interest accrual)
    interest: float = 0.0                         # cumulative interest earned on cash (Rs)


@dataclass
class Ctx:
    """Static per-run context the step needs besides today's prices."""
    calendar: list                       # trading days, sorted; must extend past every open expiry
    results: dict
    ca: dict
    sector: dict
    board: dict = field(default_factory=dict)
    known: dict = field(default_factory=dict)
    coverage: dict = field(default_factory=dict)
    market: dict = field(default_factory=dict)    # load_market()
    hedge: dict = field(default_factory=dict)     # load_hedge_chain()
    ca_adj: Optional[dict] = None                 # see Data
    ca_known: Optional[dict] = None               # see Data


def event_dates(p: Params, ctx: Ctx, sym: str, d: date) -> list:
    """Event dates the gate may use for sym on day d (sorted).

    'board' scope falls back to results when no board data is loaded, so a
    context built with results only (tests, old callers) behaves as before.
    With pit_events, a date is visible only from its announcement day; dates
    with no announcement record are treated as always known.
    """
    src = ctx.board if (p.event_scope == "board" and ctx.board) else ctx.results
    ev = src.get(sym, [])
    if p.pit_events and ctx.known:
        kn = ctx.known
        ev = [e for e in ev if kn.get((sym, e)) is None or kn[(sym, e)] <= d]
    return ev


def ca_dates(p: Params, ctx: Ctx, sym: str, d: date) -> list:
    """CA ex-dates the blackout may use for sym on day d (sorted): the scope's set, and with
    pit_ca only those already knowable on d. Contexts without the new maps (tests, old callers)
    fall back to ctx.ca; ctx.ca_known None (paper: everything stored is announced) skips the filter."""
    src = ctx.ca_adj if (p.ca_scope == "adjusting" and ctx.ca_adj is not None) else ctx.ca
    ev = src.get(sym, [])
    if p.pit_ca and ctx.ca_known is not None:     # backtest: an ex-date counts only once knowable
        kn = ctx.ca_known                         # (no announcement on record: 7 days before)
        ev = [e for e in ev if kn.get((sym, e), e - timedelta(days=7)) <= d]
    return ev


def market_on(ctx: Ctx, d: date) -> dict:
    """Latest market row (vix, vix_jump, nifty_dd) on or before d; {} if none."""
    daily = (ctx.market or {}).get("daily") or {}
    if d in daily:
        return daily[d]
    keys = ctx.market.get("_dates")
    if keys is None:
        keys = ctx.market["_dates"] = sorted(daily)
    i = bisect_right(keys, d)
    return daily[keys[i - 1]] if i else {}


def put_blocked(p: Params, ctx: Ctx, d: date) -> bool:
    """Put regime gate: NIFTY below its put_block_dma average and VIX below put_block_vix."""
    if not p.put_block_dma:
        return False
    m = market_on(ctx, d)
    dist, v = m.get(f"nifty_vs{p.put_block_dma}"), m.get("vix")
    if dist is None or dist >= 0:
        return False
    return p.put_block_vix is None or (v is not None and v < p.put_block_vix)


def brake_on(p: Params, ctx: Ctx, d: date) -> bool:
    m = market_on(ctx, d)
    if not m:
        return False
    v, j, dd = m.get("vix"), m.get("vix_jump"), m.get("nifty_dd")
    return bool((p.brake_vix_level is not None and v is not None and v > p.brake_vix_level)
                or (p.brake_vix_jump is not None and j is not None and j > p.brake_vix_jump)
                or (p.brake_nifty_dd is not None and dd is not None and dd < -p.brake_nifty_dd))


# Row tuple fed to step() for an open contract on day d:
#   (close, volume, mark, delta, short_scan, und, ask, span_vol)
#   backtest: mark = settle (SPAN price), ask = NaN
#   paper   : mark = live mid,          ask = live ask (fills at the quote)
#   span_vol: NSE SPAN file vol for the contract that day (stress-test IV); optional


def step(p: Params, st: State, d: date, ctx: Ctx, row_of, cands: Optional[pd.DataFrame],
         fill_mode: str = "slippage") -> tuple[list, list, dict]:
    """Advance the book by one session: mark + exits, hedge, then entries, then record.

    row_of(key) -> row tuple or None. cands: today's candidates (one row per
    symbol/side, as load_data builds them; an optional 'bid' column is used
    for quote fills). Returns (closed trade dicts, newly opened Pos, equity row).
    Shared verbatim by the backtest (simulate) and the paper trader.
    """
    cal = ctx.calendar
    mk = ctx.market or {}

    def td_between(a: date, b: date) -> int:
        """Trading days strictly after a up to and including b."""
        return bisect_right(cal, b) - bisect_right(cal, a)

    def session_of(e: date) -> date:
        """An event on a weekend / holiday happens before the NEXT session opens, so it counts
        as that session: results on Saturday -> exit on Friday, not Thursday."""
        i = bisect_left(cal, e)
        return cal[i] if i < len(cal) else e

    def beta_of(sym: str) -> float:
        b = asof(mk.get("beta", {}).get(sym), d)
        lo, hi = p.beta_bounds
        return min(max(b, lo), hi) if b is not None and b == b else 1.0

    closed: list = []
    opened: list = []
    rows_today: dict = {}

    def exit_ref(ps: Pos, row) -> float:
        """Exit reference price: the day's traded CLOSE when the contract traded (price_basis
        'close'), else the mark (SPAN / settle price) -- untraded exits also pay untraded_slip."""
        if p.price_basis == "close" and row is not None and row[1] > 0 and row[0] == row[0] and row[0] > 0:
            return row[0]
        return ps.mark

    def exit_slip(ps: Pos, row) -> float:
        traded = row is not None and row[1] > 0
        vol = row[1] if row is not None else 0.0
        if p.exit_cost_vol == "max":
            vol = max(vol, ps.entry_vol)
        return half_spread(p, vol) + (0.0 if traded else p.untraded_slip)

    def tradable(row) -> bool:
        """Can the position be bought back today? A trade on the day, or (paper) a live ask."""
        if row is None:
            return False
        if row[1] > 0:
            return True
        ask = row[6] if len(row) > 6 else NAN
        return fill_mode == "quote" and ask == ask and ask > 0

    def close_pos(ps: Pos, row, reason: str):
        traded = row is not None and row[1] > 0
        px = exit_ref(ps, row)
        if reason == "expiry":
            intrinsic = max(ps.strike - ps.und, 0.0) if ps.side == "PE" else max(ps.und - ps.strike, 0.0)
            fill, cost = intrinsic, 0.0
            if intrinsic > 0:                    # ITM at expiry: physical delivery of the shares
                cost = p.delivery_cost * ps.strike * ps.lot
        else:
            ask = row[6] if row is not None and len(row) > 6 else NAN
            if fill_mode == "quote" and ask == ask and ask > 0:
                fill = ask
            else:
                fill = px * (1 + exit_slip(ps, row))
            cost = order_costs(d, fill * ps.lot, "B", p.brokerage)
        pnl = (ps.credit - fill) * ps.lot - ps.entry_cost - cost
        st.cash -= fill * ps.lot + cost          # credit and entry cost were booked at entry
        closed.append(dict(
            pid=ps.pid, symbol=ps.symbol, side=ps.side, sector=ps.sector, expiry=ps.expiry,
            strike=ps.strike, lot=ps.lot, lots=ps.lots, entry_date=ps.entry_date, exit_date=d,
            entry_px=ps.entry_px, credit=ps.credit, exit_px=px, exit_fill=fill,
            costs=ps.entry_cost + cost, pnl=pnl, margin=ps.margin,
            rom=pnl / ps.margin if ps.margin else NAN,
            days=(d - ps.entry_date).days, reason=reason, traded_exit=traded, rich=ps.rich, iv_hv=ps.iv_hv))

    def rem_yield(ps: Pos) -> float:
        """Annualised premium still to earn, on current margin."""
        if ps.mark != ps.mark or ps.margin <= 0:
            return float("inf")
        return ps.mark * ps.lot / ps.margin * 365.0 / max((ps.expiry - d).days, 1)

    # ---- 1. mark + exits --------------------------------------------------
    keep = []
    for ps in st.book:
        row = row_of(ps.key)
        rows_today[ps.pid] = row
        if row is not None:
            close, vol, mark, sdelta, sscan, und = row[:6]
            ps.svol = row[7] if len(row) > 7 else NAN       # today's SPAN vol only; stale vol is not reused
            ps.mark = mark if mark == mark else (close if close == close else ps.mark)
            ps.delta = abs(sdelta) if sdelta == sdelta else ps.delta
            ps.und = und if und == und else ps.und
            if sscan == sscan and ps.und == ps.und:
                ps.margin = sscan * ps.lot * p.broker_mult + p.elm * ps.und * ps.lot
        reason = None
        left = td_between(d, ps.expiry)
        res = _next_on_or_after(event_dates(p, ctx, ps.symbol, d), d)
        cax = _next_on_or_after(ca_dates(p, ctx, ps.symbol, d), d)
        if d >= ps.expiry:
            reason = "expiry"
        elif res is not None and res <= ps.expiry and td_between(d, session_of(res)) <= p.earn_exit_lead:
            reason = "results"          # close the session before results (or today if missed)
        elif cax is not None and cax <= ps.expiry and td_between(d, session_of(cax)) <= 1:
            reason = "corp_action"      # close the session before the ex-date
        elif (p.stop_mult and row is not None and ps.mark == ps.mark
              and (row[1] > 0 or not p.stop_requires_trade)
              and ps.mark >= p.stop_mult * ps.entry_px):
            reason = "stop_mult"
        elif p.stop_spot_cross and ps.und == ps.und and (
                (ps.side == "PE" and ps.und < ps.strike) or (ps.side == "CE" and ps.und > ps.strike)):
            reason = "stop_spot"
        elif p.stop_delta and ps.delta == ps.delta and ps.delta > p.stop_delta:
            reason = "stop_delta"
        elif p.exit_hurdle is not None and rem_yield(ps) < p.exit_hurdle:
            reason = "hurdle"
        elif p.profit_residual is not None and ps.mark == ps.mark and ps.mark <= p.profit_residual * ps.entry_px:
            reason = "profit"
        elif left <= p.final_exit_td:
            reason = "final_exit"
        elif left <= p.time_exit_td and ps.delta == ps.delta and ps.delta > p.near_money_delta:
            reason = "time_exit"
        if reason is None and ps.exit_pending:
            reason = ps.exit_pending             # an earlier exit that could not be executed
        if reason and reason != "expiry" and p.untraded_exit == "defer" and not tradable(row):
            ps.exit_pending = ps.exit_pending or reason   # keep the first reason; retry next session
            keep.append(ps)
        elif reason:
            close_pos(ps, row, ps.exit_pending if (ps.exit_pending and reason != "expiry") else reason)
        else:
            keep.append(ps)
    st.book = keep

    # ---- 2. index hedge (backtest only) -------------------------------------
    hc = ctx.hedge or {}
    if p.hedge and hc:
        _hedge_step(p, st, d, hc, td_between, beta_of, closed)

    # ---- 3. capacity, drawdown, stress -------------------------------------
    # Broker ledger view: cash already holds credited premium, so subtract
    # the current buy-back liability (MTM) and add the hedge's value. Without
    # compounding, gains do not add capacity but losses still remove it.
    def ledger():
        liab = sum(ps.mark * ps.lot for ps in st.book if ps.mark == ps.mark)
        hval = sum(h.mark * h.qty for h in st.hedges if h.mark == h.mark)
        return liab, hval

    liab, hval = ledger()
    # interest on cash since the previous session (yesterday's marks; calendar days)
    if (p.cash_yield or p.cash_yield_file) and st.last_day is not None and d > st.last_day:
        # rate in force at the start of the accrual period
        accr = max(st.cash - liab + hval, 0.0) * cash_rate(p, st.last_day) * (d - st.last_day).days / 365.0
        st.cash += accr
        st.interest += accr
    st.last_day = d
    cash_eff = st.cash - liab + hval
    cash_base = cash_eff if p.compound else min(cash_eff, p.cash)
    capacity = p.collateral + cash_base * (1 - p.cash_haircut)
    used = sum(ps.margin for ps in st.book)
    equity_now = st.cash + p.collateral - liab + hval
    size_mult = 1.0
    if p.dd_halve_at is not None and st.peak > 0 and (st.peak - equity_now) / p.cash > p.dd_halve_at:
        size_mult = 0.5

    def iv_of(side, mark, und, strike, expiry, svol=NAN, last=NAN) -> float:
        return stress_iv(p, side, mark, und, strike, max((expiry - d).days, 1) / 365.0, svol, last)

    def pos_vec(side, mark, und, strike, expiry, qty, sym, iv=None) -> list:
        t = max((expiry - d).days, 1) / 365.0
        return scenario_losses(p, side, mark, und, strike, t, qty, beta_of(sym), short=True, iv=iv)

    def pos_idio(side, mark, und, strike, expiry, qty, iv=None) -> list:
        if not p.stress_idio:
            return []
        t = max((expiry - d).days, 1) / 365.0
        return scenario_losses(p, side, mark, und, strike, t, qty, 1.0, short=True, shocks=p.stress_idio, iv=iv)

    def idio_add(sym, vec, sign=1.0):
        if vec:
            cur = idio_by_sym.setdefault(sym, [0.0] * len(vec))
            for i, x in enumerate(vec):
                cur[i] += sign * x

    def idio_cap_rs() -> float:
        frac = p.stress_idio_cap if p.stress_idio_cap is not None else p.stress_cap
        return frac * max(cash_base, 0.0)

    need_stress = p.stress_cap is not None
    book_vec = [0.0] * len(p.stress_moves)
    vec_by_pid: dict = {}
    idio_by_pid: dict = {}
    idio_by_sym: dict = {}                   # symbol -> loss per single-stock shock (all its positions)
    if need_stress or p.hedge:
        for ps in st.book:
            mark, und = ps.mark, ps.und
            if not (mark == mark and und == und):
                # never silently drop a position from the stress test: price it from its
                # entry premium / strike instead (paper books can load a NULL mark)
                STRESS_NONFINITE[0] += 1
                mark = mark if mark == mark else ps.entry_px
                und = und if und == und else ps.strike
            iv = iv_of(ps.side, mark, und, ps.strike, ps.expiry, ps.svol, ps.iv)
            ps.iv = iv
            v = pos_vec(ps.side, mark, und, ps.strike, ps.expiry, ps.lot, ps.symbol, iv)
            vec_by_pid[ps.pid] = v
            for i, x in enumerate(v):
                book_vec[i] += x
            if need_stress:
                idio_by_pid[ps.pid] = pos_idio(ps.side, mark, und, ps.strike, ps.expiry, ps.lot, iv)
                idio_add(ps.symbol, idio_by_pid[ps.pid])
        und_n = hc.get("und", {}).get(d) if hc else None
        for h in st.hedges:
            if und_n and h.mark == h.mark:
                t = max((h.expiry - d).days, 1) / 365.0
                for i, x in enumerate(scenario_losses(p, "PE", h.mark, und_n, h.strike, t, h.qty, 1.0, short=False)):
                    book_vec[i] += x
    stress_cap_rs = p.stress_cap * max(cash_base, 0.0) if need_stress else float("inf")

    # stress trim: the cap only gates entries, and marks drift (the 19 Oct grid sat
    # above the cap on 701 of 1,729 days, peaking at 2.5x). Past stress_trim_at x cap,
    # close the position losing most in the book's worst scenario until back at the cap.
    if need_stress and p.stress_trim_at is not None and max(book_vec) > p.stress_trim_at * stress_cap_rs:
        while st.book and max(book_vec) > stress_cap_rs:
            w = max(range(len(book_vec)), key=lambda i: book_vec[i])
            cand_ps = [ps for ps in st.book if ps.pid in vec_by_pid]
            if not cand_ps:
                break
            worst = max(cand_ps, key=lambda ps: vec_by_pid[ps.pid][w])
            if vec_by_pid[worst.pid][w] <= 0:
                break                                    # nothing left that adds to the worst case
            close_pos(worst, rows_today.get(worst.pid), "stress_trim")
            st.book.remove(worst)
            for i, x in enumerate(vec_by_pid.pop(worst.pid)):
                book_vec[i] -= x
            idio_add(worst.symbol, idio_by_pid.pop(worst.pid, []), -1.0)
        liab, hval = ledger()
        cash_eff = st.cash - liab + hval
        cash_base = cash_eff if p.compound else min(cash_eff, p.cash)
        capacity = p.collateral + cash_base * (1 - p.cash_haircut)
        used = sum(ps.margin for ps in st.book)
        stress_cap_rs = p.stress_cap * max(cash_base, 0.0)

    # ---- 4. entries -------------------------------------------------------
    n_new = 0
    braked = brake_on(p, ctx, d)
    puts_off = put_blocked(p, ctx, d)
    blocked: list = []                       # entries refused only for lack of room (soft hurdle)
    cand_vecs: dict = {}                     # (symbol, side) -> (1-lot stress vec, idio vec, iv) from ranking
    if cands is not None and len(cands):
        rows = cands[cands["opt_type"].isin(p.sides)].copy()
        if p.symbols:
            rows = rows[rows["symbol"].isin(p.symbols)]
        if len(rows):
            col = "rich_vol" if p.rich_measure == "vol" else "rich_px"
            if col in rows.columns:
                rows["rich"] = rows[col].astype(float)
            elif p.rich_measure == "vol":                         # paper trader: computed live
                rows["rich"] = [vol_ratio(c.opt_type, float(c.premium), float(c.und), float(c.strike),
                                          max((_d(c.expiry) - d).days, 1) / 365.0, float(getattr(c, "svol", NAN)),
                                          p.rich_rate) for c in rows.itertuples(index=False)]
            else:
                rows["rich"] = [richness(c.opt_type, float(c.premium), float(c.und), float(c.strike),
                                         max((_d(c.expiry) - d).days, 1) / 365.0, float(getattr(c, "svol", NAN)),
                                         p.rich_rate) for c in rows.itertuples(index=False)]
            if p.min_rich is not None:
                rows = rows[rows["rich"] >= p.min_rich]          # NaN (no SPAN vol) fails: skipped
                for k in range(1, p.rich_confirm_days + 1):      # missing history fails: skipped
                    pc = f"{col}_p{k}"
                    rows = rows[rows[pc] >= p.min_rich] if pc in rows.columns else rows.iloc[:0]
            hvs = mk.get("hv20", {})
            atm = rows["atm_iv"] if "atm_iv" in rows.columns else pd.Series(NAN, index=rows.index)
            rows["iv_hv"] = [a / h if (h is not None and h > 0) else NAN
                             for a, h in zip(atm, (asof(hvs.get(s_), d) for s_ in rows["symbol"]))]
            if p.max_iv_hv is not None:
                rows = rows[rows["iv_hv"] <= p.max_iv_hv]        # NaN (no ATM IV / HV) fails: skipped
        if p.entry_mode == "rank_move":
            mv = rows["move"]
            rows = rows[((rows["opt_type"] == "PE") & (mv < 0)) | ((rows["opt_type"] == "CE") & (mv > 0))]
            if p.move_cap is not None:
                rows = rows[rows["move"].abs() <= p.move_cap]
            rows = rows.assign(_r=rows["move"].abs()).sort_values(_RANK_KEYS, ascending=_RANK_ASC, kind="mergesort")
        else:
            if p.move_cap is not None:
                rows = rows[rows["move"].isna() | (rows["move"].abs() <= p.move_cap)]
            if p.rank_by == "stress" and len(rows):
                # premium per rupee of worst-scenario loss, one lot each
                def _worst(c):
                    a = (c.opt_type, float(c.premium), float(c.und), float(c.strike), _d(c.expiry), int(c.lot))
                    iv = iv_of(*a[:5], getattr(c, "svol", NAN))
                    v, i = pos_vec(*a, c.symbol, iv), pos_idio(*a, iv)
                    cand_vecs[(c.symbol, c.opt_type)] = (v, i, iv)
                    return max(v + i)
                rows = rows.assign(_r=[float(c.premium) * int(c.lot) / max(_worst(c), 1.0)
                                       for c in rows.itertuples(index=False)]).sort_values(
                                           _RANK_KEYS, ascending=_RANK_ASC, kind="mergesort")
            else:
                rows = rows.assign(_r=rows["rom"]).sort_values(_RANK_KEYS, ascending=_RANK_ASC, kind="mergesort")
        has_bid = "bid" in rows.columns
        per_trade = p.trade_margin_pct * size_mult / max(p.ladder_entries, 1)

        def try_enter(c) -> str:
            """'ok' | 'room' (would fit with more margin/stress room) | 'skip'."""
            nonlocal used, n_new
            sym, side, exp = c.symbol, c.opt_type, _d(c.expiry)
            if braked and side in p.brake_sides:
                return "skip"
            if puts_off and side == "PE":
                return "skip"
            mine = [ps for ps in st.book if ps.symbol == sym and ps.side == side]
            if len(mine) >= max(p.max_lots_per_symbol_side, p.ladder_entries):
                return "skip"
            if mine and p.ladder_entries > 1 and \
                    td_between(max(ps.entry_date for ps in mine), d) < p.ladder_min_gap_days:
                return "skip"
            if side == "CE" and (p.call_trend_dist is not None or p.call_trend_ret is not None):
                tr = asof(mk.get("trend", {}).get(sym), d)
                if tr is not None:
                    dist, r20 = tr
                    if p.call_trend_dist is not None and dist == dist and dist > p.call_trend_dist:
                        return "skip"
                    if p.call_trend_ret is not None and r20 == r20 and r20 > p.call_trend_ret:
                        return "skip"
            if p.require_event_coverage and ctx.coverage:
                cov = ctx.coverage.get(sym)
                if cov is None or d < cov - timedelta(days=100):
                    return "skip"                        # no event history yet: unknown, not event-free
            nxt = _next_on_or_after(event_dates(p, ctx, sym, d), d)
            if nxt is not None and td_between(d, session_of(nxt)) <= p.earn_entry_days:
                return "skip"
            cax = _next_on_or_after(ca_dates(p, ctx, sym, d), d)
            if cax is not None and cax <= exp:
                return "skip"
            m1 = float(c.margin)                         # margin for ONE lot
            vol = float(c.volume) if c.volume == c.volume else 0.0
            if p.sizing == "pct":
                lots = int(per_trade * capacity // m1) if m1 > 0 else 0
                vol_lim = int(p.max_vol_frac * vol)
                lots = min(lots, p.max_lots_per_trade, max(vol_lim, 1))
                if vol < p.multi_lot_min_volume:
                    lots = min(lots, 1)
                if lots < 1:
                    return "skip"                        # one lot already over the per-trade cap
            else:
                lots = 1
            sec = ctx.sector.get(sym, "OTHER")
            sec_used = sum(ps.margin for ps in st.book if ps.sector == sec)
            room = min(p.util_cap * capacity - used, p.sector_cap * p.util_cap * capacity - sec_used)
            if p.sizing == "pct":
                lots = min(lots, int(room // m1) if m1 > 0 else 0)  # shrink to fit caps
            if lots < 1 or lots * m1 > room:
                return "room"
            n1 = float(c.lot) * float(c.strike)             # notional of ONE lot
            if p.max_symbol_notional is not None and n1 > 0:
                cur = sum(ps.lot * ps.strike for ps in st.book if ps.symbol == sym)
                lots = min(lots, int((p.max_symbol_notional * max(cash_base, 0.0) - cur) // n1))
            if p.max_put_notional is not None and side == "PE" and n1 > 0:
                cur = sum(ps.lot * ps.strike for ps in st.book if ps.side == "PE")
                lots = min(lots, int((p.max_put_notional * (max(cash_base, 0.0) + p.collateral) - cur) // n1))
            if lots < 1:
                return "skip"                            # one lot would breach a notional cap
            cvec = ivec = None
            if need_stress:
                if (sym, side) in cand_vecs:
                    cvec, ivec, civ = cand_vecs[(sym, side)]
                else:
                    a = (side, float(c.premium), float(c.und), float(c.strike), exp, int(c.lot))
                    civ = iv_of(*a[:5], getattr(c, "svol", NAN))
                    cvec, ivec = pos_vec(*a, sym, civ), pos_idio(*a, civ)
                icur = idio_by_sym.get(sym, [0.0] * len(ivec))
                icap = idio_cap_rs()

                def over(n):
                    return max(b + n * x for b, x in zip(book_vec, cvec)) > stress_cap_rs or \
                        (ivec and max(b + n * x for b, x in zip(icur, ivec)) > icap)
                while lots >= 1 and over(lots):
                    if p.sizing != "pct":
                        lots = 0
                        break
                    lots -= 1
                if lots < 1:
                    return "room"
            m = m1 * lots
            lot = int(c.lot) * lots
            bid = float(c.bid) if has_bid else NAN
            if fill_mode == "quote" and bid == bid and bid > 0:
                credit = bid
            else:
                credit = float(c.premium) * (1 - half_spread(p, vol))
            ecost = order_costs(d, credit * lot, "S", p.brokerage)
            st.cash += credit * lot - ecost
            st.pid += 1
            ps = Pos(st.pid, sym, c.raw, side, exp, float(c.strike), lot, d, float(c.premium),
                     credit, ecost, m, sec, mark=float(c.premium), delta=float(c.delta), und=float(c.und),
                     lots=lots, entry_vol=vol, svol=float(getattr(c, "svol", NAN)),
                     rich=float(getattr(c, "rich", NAN)), iv_hv=float(getattr(c, "iv_hv", NAN)),
                     iv=civ if cvec is not None else NAN)
            st.book.append(ps)
            opened.append(ps)
            used += m
            if cvec is not None:
                for i, x in enumerate(cvec):
                    book_vec[i] += lots * x
                idio_add(sym, [lots * x for x in ivec])
            n_new += 1
            return "ok"

        for c in rows.itertuples(index=False):
            if n_new >= p.max_new_per_day:
                break
            if try_enter(c) == "room":
                blocked.append(c)

        # soft hurdle: free capacity from tired positions only for an entry that is waiting
        if p.hurdle_swap is not None and blocked and n_new < p.max_new_per_day:
            tired = sorted((ps for ps in st.book if ps not in opened and rem_yield(ps) < p.hurdle_swap),
                           key=rem_yield)
            for c in blocked:
                if n_new >= p.max_new_per_day or not tired:
                    break
                while tired:
                    ps = tired.pop(0)
                    close_pos(ps, rows_today.get(ps.pid), "swap")
                    st.book.remove(ps)
                    used -= ps.margin
                    if need_stress and ps.pid in vec_by_pid:
                        for i, x in enumerate(vec_by_pid.pop(ps.pid)):
                            book_vec[i] -= x
                        idio_add(ps.symbol, idio_by_pid.pop(ps.pid, []), -1.0)
                    liab, hval = ledger()
                    cash_base = (st.cash - liab + hval) if p.compound else min(st.cash - liab + hval, p.cash)
                    capacity = p.collateral + cash_base * (1 - p.cash_haircut)
                    if need_stress:
                        stress_cap_rs = p.stress_cap * max(cash_base, 0.0)
                    if try_enter(c) == "ok":
                        break

    # ---- 5. record --------------------------------------------------------
    liab, hval = ledger()
    cash_eff = st.cash - liab + hval
    equity = st.cash + p.collateral - liab + hval
    st.peak = max(st.peak, equity)
    eq = dict(date=d, equity=equity, cash=st.cash, liability=liab, used_margin=used,
              # recorded capacity = collateral + cash, WITHOUT cash_haircut, so drawdown / stress
              # percentages keep a cash base; the trading capacity above applies the haircut
              capacity=p.collateral + cash_eff, n_open=len(st.book), n_new=n_new,
              breach=used > p.collateral + cash_eff, cash_rule_breach=cash_eff < 0.5 * used,
              stress=max(0.0, max(book_vec)) if (need_stress or p.hedge) else NAN,
              stress_cap=stress_cap_rs if need_stress else NAN, brake=braked, hedge_value=hval,
              interest=st.interest,
              stress_idio=max([0.0] + [max(v) for v in idio_by_sym.values()]) if p.stress_idio else NAN)
    return closed, opened, eq


def _hedge_step(p: Params, st: State, d: date, hc: dict, td_between, beta_of, closed: list) -> None:
    """Mark, roll and resize the long NIFTY put against today's short-put book."""
    und = hc["und"].get(d)
    for h in st.hedges:
        h.mark = hc["px"].get((h.expiry, h.strike), {}).get(d, h.mark)

    def close_hedge(h: Hedge, reason: str):
        if d >= h.expiry and und:
            fill, cost = max(h.strike - und, 0.0), 0.0
        else:
            fill = h.mark * (1 - p.hedge_slip)
            cost = order_costs(d, fill * h.qty, "S", p.brokerage)
        st.cash += fill * h.qty - cost
        pnl = (fill - h.paid) * h.qty - h.entry_cost - cost
        closed.append(dict(
            pid=-h.hid, symbol="NIFTY", side="HEDGE", sector="HEDGE", expiry=h.expiry, strike=h.strike,
            lot=h.qty, lots=1, entry_date=h.entry_date, exit_date=d, entry_px=h.paid, credit=-h.paid,
            exit_px=h.mark, exit_fill=fill, costs=h.entry_cost + cost, pnl=pnl, margin=h.paid * h.qty,
            rom=pnl / (h.paid * h.qty) if h.paid * h.qty else NAN, days=(d - h.entry_date).days,
            reason=reason, traded_exit=True))
        st.hedges.remove(h)

    for h in list(st.hedges):
        if d >= h.expiry or td_between(d, h.expiry) <= p.hedge_roll_td:
            close_hedge(h, "hedge_roll")
    if not und:
        return
    target = p.hedge_ratio * sum(beta_of(ps.symbol) * ps.und * ps.lot
                                 for ps in st.book if ps.side == "PE" and ps.und == ps.und) / und
    have = sum(h.qty for h in st.hedges)
    if target <= 0 or (have > 0 and abs(have - target) / target <= 0.5):
        return
    for h in list(st.hedges):
        close_hedge(h, "hedge_resize")
    want_k = und * (1 - p.hedge_otm)
    best = None
    for (e, k), series in hc["px"].items():
        dte = (e - d).days
        px = series.get(d)
        if px is None or px <= 0 or not (p.hedge_dte_lo <= dte <= p.hedge_dte_hi):
            continue
        score = (dte, abs(k - want_k))          # nearest eligible expiry, then closest strike
        if best is None or score < best[0]:
            best = (score, e, k, px)
    if best is None:
        return
    _, e, k, px = best
    paid = px * (1 + p.hedge_slip)
    cost = order_costs(d, paid * target, "B", p.brokerage)
    st.cash -= paid * target + cost
    st.pid += 1
    st.hedges.append(Hedge(st.pid, e, k, target, d, paid, cost, px))


def simulate(p: Params, data: Data) -> Result:
    cal = [d for d in data.calendar if p.start <= d <= p.end]
    ctx = Ctx(data.calendar, data.results, data.ca, data.sector, data.board, data.known, data.coverage,
              data.market, data.hedge, data.ca_adj, data.ca_known)
    cand_by_day = ({d: g for d, g in data.cands.groupby("trade_date")}
                   if len(data.cands) and "trade_date" in data.cands else {})
    st = State(cash=p.cash)
    trades, eq_rows = [], []
    nonfinite0 = STRESS_NONFINITE[0]
    for d in cal:
        def row_of(k, _d=d):
            return data.paths.get(k, {}).get(_d)
        closed, _, eq = step(p, st, d, ctx, row_of, cand_by_day.get(d))
        trades.extend(closed)
        eq_rows.append(eq)

    # force-close anything left at the end of the window (mark-to-market)
    if (st.book or st.hedges) and cal:
        last = cal[-1]
        for ps in st.book:
            row = data.paths.get(ps.key, {}).get(last)
            traded = row is not None and row[1] > 0
            vol = row[1] if row is not None else 0.0
            ref = row[0] if (p.price_basis == "close" and traded and row[0] == row[0] and row[0] > 0) else ps.mark
            vcost = vol if p.exit_cost_vol == "exit" else max(vol, ps.entry_vol)   # as exit_slip()
            fill = ref * (1 + half_spread(p, vcost) + (0.0 if traded else p.untraded_slip))
            cost = order_costs(last, fill * ps.lot, "B", p.brokerage)
            pnl = (ps.credit - fill) * ps.lot - ps.entry_cost - cost
            st.cash -= fill * ps.lot + cost
            trades.append(dict(
                pid=ps.pid, symbol=ps.symbol, side=ps.side, sector=ps.sector, expiry=ps.expiry,
                strike=ps.strike, lot=ps.lot, lots=ps.lots, entry_date=ps.entry_date, exit_date=last,
                entry_px=ps.entry_px, credit=ps.credit, exit_px=ref, exit_fill=fill,
                costs=ps.entry_cost + cost, pnl=pnl, margin=ps.margin,
                rom=pnl / ps.margin if ps.margin else NAN, days=(last - ps.entry_date).days,
                reason="window_end", traded_exit=traded, rich=ps.rich, iv_hv=ps.iv_hv))
        for h in st.hedges:
            fill = h.mark * (1 - p.hedge_slip)
            cost = order_costs(last, fill * h.qty, "S", p.brokerage)
            st.cash += fill * h.qty - cost
            pnl = (fill - h.paid) * h.qty - h.entry_cost - cost
            trades.append(dict(
                pid=-h.hid, symbol="NIFTY", side="HEDGE", sector="HEDGE", expiry=h.expiry, strike=h.strike,
                lot=h.qty, lots=1, entry_date=h.entry_date, exit_date=last, entry_px=h.paid, credit=-h.paid,
                exit_px=h.mark, exit_fill=fill, costs=h.entry_cost + cost, pnl=pnl, margin=h.paid * h.qty,
                rom=pnl / (h.paid * h.qty) if h.paid * h.qty else NAN, days=(last - h.entry_date).days,
                reason="window_end", traded_exit=True))
        st.book, st.hedges = [], []
        eq_rows[-1]["equity"] = st.cash + p.collateral

    tr = pd.DataFrame(trades)
    eq = pd.DataFrame(eq_rows)
    m = compute_metrics(p, tr, eq)
    m["stress_unpriced"] = STRESS_NONFINITE[0] - nonfinite0     # fail-closed stress losses (should be 0)
    return Result(p, tr, eq, m)


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

def compute_metrics(p: Params, tr: pd.DataFrame, eq: pd.DataFrame) -> dict:
    m: dict = {"trades": int(len(tr))}
    if eq.empty:
        return m
    e = eq.set_index(pd.to_datetime(eq["date"]))["equity"]
    years = max((e.index[-1] - e.index[0]).days / 365.25, 1e-9)
    total0 = p.cash + p.collateral
    pnl = e.iloc[-1] - total0
    m["pnl"] = round(pnl)                                  # equity change: trading P&L + interest
    if "interest" in eq and eq["interest"].notna().any():
        m["interest_rs"] = round(float(eq["interest"].iloc[-1]))
        m["trading_pnl"] = round(pnl - m["interest_rs"])
    m["cagr_total"] = round((e.iloc[-1] / total0) ** (1 / years) - 1, 4)
    m["cagr_on_cash"] = round(((p.cash + pnl) / p.cash) ** (1 / years) - 1, 4) if p.cash + pnl > 0 else -1.0
    dd = e - e.cummax()
    m["max_dd_rs"] = round(dd.min())
    m["max_dd_pct_total"] = round(dd.min() / total0, 4)
    m["max_dd_pct_cash"] = round(dd.min() / p.cash, 4)
    daily = e.diff().dropna()
    if len(daily):
        q = daily.quantile(0.05)
        m["cvar95_daily_rs"] = round(daily[daily <= q].mean())
        m["worst_day_rs"] = round(daily.min())
    monthly = e.resample("ME").last().diff().dropna()
    if len(monthly):
        m["worst_month_rs"] = round(monthly.min())
        m["pct_months_negative"] = round(float((monthly < 0).mean()), 3)
    m["calmar"] = round((pnl / years) / abs(dd.min()), 2) if dd.min() < 0 else None
    m["avg_util"] = round(float((eq["used_margin"] / (p.cash + p.collateral)).mean()), 3)
    m["max_util_of_capacity"] = round(float((eq["used_margin"] / eq["capacity"].clip(lower=1)).max()), 3)
    m["breach_days"] = int(eq["breach"].sum())
    m["cash_rule_breach_days"] = int(eq["cash_rule_breach"].sum())
    # the stress test that matters: Feb-Jun 2020, drawdown measured from the pre-crash peak
    cov = e[(e.index >= "2020-01-01") & (e.index <= "2020-06-30")]
    if len(cov):
        pre = e[e.index <= "2020-06-30"]
        m["covid_dd_pct_cash"] = round(float((pre - pre.cummax()).loc[cov.index].min()) / p.cash, 4)
    if "stress" in eq and eq["stress"].notna().any():
        st_pct = eq["stress"] / (eq["capacity"] - p.collateral).clip(lower=1)
        m["avg_stress_pct_cash"] = round(float(st_pct.mean()), 3)
        m["max_stress_pct_cash"] = round(float(st_pct.max()), 3)
    if "stress_idio" in eq and eq["stress_idio"].notna().any():
        si = eq["stress_idio"] / (eq["capacity"] - p.collateral).clip(lower=1)
        m["max_idio_pct_cash"] = round(float(si.max()), 3)
    if "brake" in eq:
        m["brake_days"] = int(eq["brake"].fillna(False).astype(bool).sum())
    if len(tr) and "side" in tr and (tr["side"] == "HEDGE").any():
        m["hedge_pnl"] = round(float(tr.loc[tr["side"] == "HEDGE", "pnl"].sum()))
    tr = tr[tr["side"] != "HEDGE"] if len(tr) and "side" in tr else tr
    if len(tr):
        m["win_rate"] = round(float((tr["pnl"] > 0).mean()), 3)
        m["avg_pnl"] = round(float(tr["pnl"].mean()))
        m["avg_rom_per_trade"] = round(float(tr["rom"].mean()), 4)
        m["p1_trade_rs"] = round(float(tr["pnl"].quantile(0.01)))
        m["worst_trade_rs"] = round(float(tr["pnl"].min()))
        m["avg_days"] = round(float(tr["days"].mean()), 1)
        m["costs_share_of_gross"] = round(float(tr["costs"].sum() / max(tr["credit"].mul(tr["lot"]).sum(), 1)), 3)
        m["exit_mix"] = tr["reason"].value_counts(normalize=True).round(3).to_dict()
        m["pnl_by_side"] = tr.groupby("side")["pnl"].sum().round().to_dict()
        m["trades_by_side"] = {k: int(v) for k, v in tr.groupby("side").size().items()}
        stp = tr[tr["reason"] == "stop_mult"]
        m["stop_pnl_by_side"] = stp.groupby("side")["pnl"].sum().round().to_dict()
        if "hedge_pnl" in m:
            m["pnl_by_side"]["HEDGE"] = m["hedge_pnl"]
        m["pnl_by_year"] = tr.groupby(pd.to_datetime(tr["exit_date"]).dt.year)["pnl"].sum().round().to_dict()
    return m


# ---------------------------------------------------------------------------
# reports: per stock, per period, persistence
# ---------------------------------------------------------------------------

def stock_table(tr: pd.DataFrame) -> pd.DataFrame:
    """One row per stock. P&L is per LOT (pnl / lots) so pct-sized runs compare fairly."""
    if tr.empty:
        return pd.DataFrame()
    t = tr.copy()
    t["lots"] = t["lots"].fillna(1).clip(lower=1) if "lots" in t else 1
    t["pnl1"] = t["pnl"] / t["lots"]
    t["m1"] = t["margin"] / t["lots"]
    t["yr"] = pd.to_datetime(t["exit_date"]).dt.year
    g = t.groupby("symbol")
    out = pd.DataFrame({
        "sector": g["sector"].first(),
        "trades": g.size(),
        "pnl_per_lot": g["pnl1"].sum().round(),
        "win": g["pnl"].apply(lambda x: (x > 0).mean()).round(2),
        "rom_pct": (100 * g["pnl1"].sum() / g["m1"].sum()).round(2),
        "stop_rate": g["reason"].apply(lambda x: x.str.startswith("stop").mean()).round(2),
        "worst": g["pnl1"].min().round(),
        "puts": t[t.side == "PE"].groupby("symbol")["pnl1"].sum().round(),
        "calls": t[t.side == "CE"].groupby("symbol")["pnl1"].sum().round(),
    })
    yearly = t.pivot_table(index="symbol", columns="yr", values="pnl1", aggfunc="sum")
    out["yrs_positive"] = (yearly > 0).sum(axis=1).astype(int).astype(str) + "/" + yearly.notna().sum(axis=1).astype(str)
    out = out.join(yearly.round().add_prefix("y"))
    return out.sort_values("pnl_per_lot", ascending=False)


def persistence(tr: pd.DataFrame, split: date) -> dict:
    """Does a stock's P&L before `split` predict its P&L after? (per lot)"""
    if tr.empty:
        return {}
    t = tr.copy()
    t["pnl1"] = t["pnl"] / (t["lots"].fillna(1).clip(lower=1) if "lots" in t else 1)
    t["half"] = (pd.to_datetime(t["exit_date"]).dt.date >= split).map({False: "before", True: "after"})
    pv = t.pivot_table(index="symbol", columns="half", values="pnl1", aggfunc="sum").dropna()
    if len(pv) < 5 or not {"before", "after"} <= set(pv.columns):
        return {"stocks": len(pv)}
    top = pv["before"].rank(ascending=False) <= max(len(pv) // 4, 1)
    return {
        "stocks": int(len(pv)),
        "pnl_corr": round(float(pv["before"].corr(pv["after"])), 2),
        "rank_corr": round(float(pv["before"].rank().corr(pv["after"].rank())), 2),
        "sign_agreement": round(float(((pv["before"] > 0) == (pv["after"] > 0)).mean()), 2),
        "top_quartile_before_avg_after": round(float(pv.loc[top, "after"].mean())),
        "rest_avg_after": round(float(pv.loc[~top, "after"].mean())),
    }


def print_report(label: str, metrics: dict, tr: pd.DataFrame, top: int = 12, split: Optional[date] = None):
    keys = ["trades", "pnl", "cagr_on_cash", "cagr_total", "max_dd_rs", "max_dd_pct_total", "worst_month_rs",
            "pct_months_negative", "win_rate", "calmar", "avg_util", "breach_days", "cash_rule_breach_days",
            "covid_dd_pct_cash", "max_stress_pct_cash", "brake_days", "hedge_pnl", "stress_unpriced",
            "interest_rs", "trading_pnl"]
    print(f"\n=== {label} ===")
    print("  " + "  ".join(f"{k}={metrics.get(k)}" for k in keys))
    if tr.empty:
        return
    yr = tr.assign(yr=pd.to_datetime(tr["exit_date"]).dt.year).pivot_table(
        index="yr", columns="side", values="pnl", aggfunc="sum").round()
    yr["total"] = yr.sum(axis=1)
    print("\n  P&L by year and side (Rs):\n  " + yr.to_string().replace("\n", "\n  "))
    tr = tr[tr["side"] != "HEDGE"]
    st = stock_table(tr)
    cols = ["sector", "trades", "pnl_per_lot", "win", "rom_pct", "stop_rate", "puts", "calls", "yrs_positive", "worst"]
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(f"\n  Best {top} stocks (P&L per lot):\n  " + st[cols].head(top).to_string().replace("\n", "\n  "))
        print(f"\n  Worst {top} stocks:\n  " + st[cols].tail(top).to_string().replace("\n", "\n  "))
    if split is None:
        dts = pd.to_datetime(tr["exit_date"])
        split = (dts.min() + (dts.max() - dts.min()) / 2).date()
    print(f"\n  Persistence (before vs after {split}): {persistence(tr, split)}")
    print("  (pnl_corr / rank_corr near 0 and sign_agreement near 0.5 = past stock P&L does not predict future)")


def load_run(run: str = "latest", db: Path = WHEEL_DB) -> tuple[str, str, dict, pd.DataFrame]:
    import duckdb
    con = duckdb.connect(str(db), read_only=True)
    try:
        if run == "latest":
            row = con.execute("SELECT run_id, label, metrics FROM runs ORDER BY created DESC LIMIT 1").fetchone()
        else:
            row = con.execute("SELECT run_id, label, metrics FROM runs WHERE run_id = ? OR label = ? "
                              "ORDER BY created DESC LIMIT 1", [run, run]).fetchone()
        if row is None:
            raise SystemExit(f"no run matching {run!r}")
        if "flags" in [r[0] for r in con.execute("DESCRIBE runs").fetchall()]:
            fl = con.execute("SELECT flags FROM runs WHERE run_id = ?", [row[0]]).fetchone()[0]
            if fl:
                row = (row[0], f"{row[1]}  [FLAGGED: {fl} -- not a valid reference]", row[2])
        tr = con.execute("SELECT * FROM trades WHERE run_id = ?", [row[0]]).df()
    finally:
        con.close()
    return row[0], row[1], json.loads(row[2]), tr


def run_split(base: Params, split: date, label: str = "split") -> dict:
    """Same config, two windows, fresh capital each: settings chosen on one, checked on the other."""
    full = load_data(base)
    out = {}
    for tag, (a, b) in {"IS": (base.start, split - timedelta(days=1)), "OOS": (split, base.end)}.items():
        r = simulate(replace(base, start=a, end=b), full)
        rid = save(r, label=f"{label} {tag} {a}..{b}")
        out[tag] = (rid, r)
        print_report(f"{label} {tag}  {a} .. {b}  ({rid})", r.metrics, r.trades, top=8)
    both = pd.concat([out["IS"][1].trades, out["OOS"][1].trades], ignore_index=True)
    print(f"\n=== {label}: stock persistence IS -> OOS ===\n  {persistence(both, split)}")
    return out


# ---------------------------------------------------------------------------
# persistence + grids
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (run_id VARCHAR PRIMARY KEY, created TIMESTAMP, label VARCHAR,
    params JSON, metrics JSON);
CREATE TABLE IF NOT EXISTS trades (run_id VARCHAR, pid INTEGER, symbol VARCHAR, side VARCHAR, sector VARCHAR,
    expiry DATE, strike DOUBLE, lot INTEGER, lots INTEGER, entry_date DATE, exit_date DATE, entry_px DOUBLE,
    credit DOUBLE, exit_px DOUBLE, exit_fill DOUBLE, costs DOUBLE, pnl DOUBLE, margin DOUBLE,
    rom DOUBLE, days INTEGER, reason VARCHAR, traded_exit BOOLEAN);
CREATE TABLE IF NOT EXISTS equity (run_id VARCHAR, date DATE, equity DOUBLE, cash DOUBLE,
    liability DOUBLE, used_margin DOUBLE, capacity DOUBLE, n_open INTEGER, n_new INTEGER,
    breach BOOLEAN, cash_rule_breach BOOLEAN);
"""
EQUITY_ADDED = (("stress", "DOUBLE"), ("stress_cap", "DOUBLE"), ("brake", "BOOLEAN"), ("hedge_value", "DOUBLE"),
                ("stress_idio", "DOUBLE"), ("interest", "DOUBLE"))


def run_flags(params: dict) -> Optional[str]:
    """Known defects of a run's configuration; a flagged run must not be used as a reference.

    settle_fills  fills at bhavcopy SETTLE (SPAN theoretical price when the option did not
                  trade late; ~11% of entries priced at ~2x the day's high). Runs before
                  22 Oct 2026, or price_basis='settle'.
    survivorship  universe = today's index list applied to every date (runs before the
                  point-in-time universe of 22 Oct 2026, or universe='current').
    ca_lookahead  corporate-action blackout used ex-dates not yet announced (runs before
                  28 Oct 2026, or pit_ca=False); those runs also blocked every ex-date.
    """
    f = []
    if params.get("price_basis", "settle") != "close":
        f.append("settle_fills")
    if params.get("universe", "current") != "pit":
        f.append("survivorship")
    if not params.get("pit_ca", False):
        f.append("ca_lookahead")
    return ",".join(f) or None


def flag_runs(db: Path = WHEEL_DB) -> None:
    """(Re)compute `flags` for every stored run from its params."""
    import duckdb
    con = duckdb.connect(str(db))
    try:
        con.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS flags VARCHAR")
        rows = con.execute("SELECT run_id, params FROM runs").fetchall()
        for rid, prm in rows:
            con.execute("UPDATE runs SET flags = ? WHERE run_id = ?", [run_flags(json.loads(prm)), rid])
        print(con.execute("""SELECT coalesce(flags, '(clean)') AS flags, count(*) AS runs,
                                    min(created)::DATE AS first, max(created)::DATE AS last
                             FROM runs GROUP BY 1 ORDER BY 2 DESC""").df().to_string(index=False))
    finally:
        con.close()


def save(res: Result, label: str = "", db: Path = WHEEL_DB) -> str:
    import duckdb
    run_id = f"{datetime.now():%Y%m%d%H%M%S}-{res.params.key()}"
    con = duckdb.connect(str(db))
    try:
        con.execute(SCHEMA)
        con.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS lots INTEGER")   # pre-sizing DBs
        con.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS rich DOUBLE")    # pre-23-Oct DBs
        con.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS iv_hv DOUBLE")   # pre-26-Oct DBs
        for col, typ in EQUITY_ADDED:                                             # pre-risk-layer DBs
            con.execute(f"ALTER TABLE equity ADD COLUMN IF NOT EXISTS {col} {typ}")
        con.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS flags VARCHAR")
        prm = asdict(res.params)
        con.execute("INSERT INTO runs (run_id, created, label, params, metrics, flags) VALUES (?, now(), ?, ?, ?, ?)",
                    [run_id, label, json.dumps(prm, default=str), json.dumps(res.metrics, default=str),
                     run_flags(json.loads(json.dumps(prm, default=str)))])
        for name, df in (("trades", res.trades), ("equity", res.equity)):
            if len(df):
                con.register("_x", df.assign(run_id=run_id))
                have = set(df.columns) | {"run_id"}
                cols = [r[0] for r in con.execute(f"DESCRIBE {name}").fetchall()]
                sel = [c if c in have else f"NULL AS {c}" for c in cols]
                con.execute(f"INSERT INTO {name} SELECT {', '.join(sel)} FROM _x")
                con.unregister("_x")
    finally:
        con.close()
    return run_id


DEFAULT_RATE_FILE = Path(os.environ.get("CASH_RATES", str(Path(__file__).resolve().parent / "data" / "cash_rates.csv")))

BRAKE_ON = dict(brake_vix_level=25.0, brake_vix_jump=0.30, brake_nifty_dd=0.05)

GRIDS = {
    # each grid sweeps one dimension on top of the CLI base
    "stops": [dict(stop_mult=None), dict(stop_mult=1.5), dict(stop_mult=2.0), dict(stop_mult=3.0),
              dict(stop_mult=None, stop_spot_cross=True), dict(stop_mult=None, stop_delta=0.30),
              dict(stop_mult=None, stop_delta=0.40), dict(stop_mult=3.0, stop_spot_cross=True)],
    "rom": [dict(rom_target=r) for r in (0.015, 0.02, 0.025, 0.03)],        # reloads data per value
    "delta": [dict(delta_cap=x) for x in (0.15, 0.20, 0.25, 0.30)],           # reloads data per value
    "sides": [dict(sides=("PE", "CE")), dict(sides=("PE",)), dict(sides=("CE",))],
    "dte": [dict(dte_lo=30, dte_hi=35), dict(dte_lo=25, dte_hi=35), dict(dte_lo=21, dte_hi=35)],
    "pace": [dict(max_new_per_day=n) for n in (2, 4, 6, 8)],
    "sizing": [dict(sizing="fixed"), dict(sizing="pct", trade_margin_pct=0.05),
               dict(sizing="pct", trade_margin_pct=0.075), dict(sizing="pct", trade_margin_pct=0.10)],
    # item 12: take profit once the mark falls to this fraction of the entry premium (0.5 = 50% kept)
    "profit": [dict(profit_residual=None), dict(profit_residual=0.50), dict(profit_residual=0.35),
               dict(profit_residual=0.20), dict(profit_residual=0.10)],
    "entry": [dict(entry_mode="rank_move"), dict(entry_mode="all"),
              dict(entry_mode="rank_move", move_cap=None), dict(entry_mode="all", max_new_per_day=4)],
    "util": [dict(util_cap=u) for u in (0.3, 0.4, 0.5, 0.6)],
    # ---- optimization plan (Oct 2026), in its order -----------------------
    # item P: portfolio risk layer. First row = the engine before the layer.
    "risk": [dict(stress_cap=None),                                          # no risk layer
             dict(stress_cap=None, **BRAKE_ON),                              # market brake only
             dict(stress_cap=0.10), dict(stress_cap=0.15), dict(stress_cap=0.20),   # stress cap only
             dict(stress_cap=0.15, **BRAKE_ON)],
    "trim": [dict(stress_trim_at=None), dict(stress_trim_at=2.0), dict(stress_trim_at=1.5),
             dict(stress_trim_at=1.25)],
    "rank": [dict(rank_by="rom"), dict(rank_by="stress")],
    # survivorship: point-in-time index membership vs today's list applied to every date
    "universe": [dict(universe="pit"), dict(universe="current")],
    "ivsrc": [dict(iv_source="span"), dict(iv_source="implied")],
    "pxbasis": [dict(price_basis="close"), dict(price_basis="settle")],
    # interest on cash: none / T-bill-like 6.5% / 6.5% with a 10% liquid-fund margin haircut
    "cashyield": [dict(cash_yield=0.0), dict(cash_yield=0.065), dict(cash_yield=0.065, cash_haircut=0.10),
                  dict(cash_yield_file=str(DEFAULT_RATE_FILE), cash_haircut=0.10)],
    # IV-rich entry gate: premium / model price at SPAN vol
    "rich": [dict(min_rich=None), dict(min_rich=1.0), dict(min_rich=1.1), dict(min_rich=1.25), dict(min_rich=1.5)],
    # single-stock shocks (21 Oct): does charging calls for a one-name +20% gap fix the call tilt?
    "idio": [dict(stress_idio=()),
             dict(stress_idio=((0.20, 1.3),)),                                  # ranking + 15% name cap
             dict(stress_idio=((0.20, 1.3),), stress_idio_cap=0.08),
             dict(stress_idio=((0.20, 1.3),), stress_idio_cap=0.05),
             dict(stress_idio=((0.20, 1.3), (-0.20, 1.3)), stress_idio_cap=0.08)],
    # item 6: put regime gate (no new puts in an early decline)
    "putregime": [dict(put_block_dma=None), dict(put_block_dma=50), dict(put_block_dma=50, put_block_vix=20.0),
                  dict(put_block_dma=200, put_block_vix=20.0)],
    # item 10: ATM IV / HV20 entry gate
    # item 5: overfitting check -- every combination of the settings tuned so far (24 runs, two
    # data loads); daily equity feeds the CSCV / walk-forward / bootstrap analysis
    "overfit": [dict(dte_lo=21, dte_hi=dh, min_rich=mr, stop_mult=sm)
                for dh in (35, 45) for mr in (None, 1.0, 1.1, 1.25) for sm in (2.0, 3.0, None)],
    # item 11: which qualifying strike to sell
    "pick": [dict(pick_by="delta"), dict(pick_by="rich"), dict(pick_by="rom")],    # reloads data per value
    # 27 Oct: richness measure, delta band + richest strike with a ROM floor, confirmation
    # (run on the gate-1.1 base: --min-rich 1.1; reloads data per min_delta / pick_by)
    "richsel": [dict(rich_measure="price", min_rich=1.1),
                dict(rich_measure="vol", min_rich=1.0), dict(rich_measure="vol", min_rich=1.1),
                dict(rich_measure="vol", min_rich=1.1, min_delta=0.05),
                dict(rich_measure="vol", min_rich=1.1, min_delta=0.05, pick_by="volrich"),
                dict(rich_measure="vol", min_rich=1.1, min_delta=0.08, pick_by="volrich"),
                dict(rich_measure="vol", min_rich=1.1, min_delta=0.05, pick_by="volrich", rom_target=0.015),
                dict(rich_measure="vol", min_rich=1.1, min_delta=0.05, pick_by="volrich", rom_target=0.025)],
    # 27 Oct: notional caps (physical-delivery exposure), on the gate-1.1 base
    "notional": [dict(max_symbol_notional=None, max_put_notional=None),
                 dict(max_symbol_notional=2.0), dict(max_symbol_notional=1.0), dict(max_symbol_notional=0.5),
                 dict(max_put_notional=1.5), dict(max_put_notional=1.0),
                 dict(max_symbol_notional=1.0, max_put_notional=1.0)],
    # 28 Oct: corporate-action blackout fix. Row 1 = the pre-fix rule (every ex-date, known ahead)
    "ca": [dict(ca_scope="all", pit_ca=False), dict(ca_scope="all", pit_ca=True), dict(ca_scope="adjusting", pit_ca=True)],
    "confirm": [dict(rich_confirm_days=0), dict(rich_confirm_days=1), dict(rich_confirm_days=2)],
    "ivhv": [dict(max_iv_hv=None), dict(max_iv_hv=1.25), dict(max_iv_hv=1.0), dict(max_iv_hv=0.8)],
    "hedge": [dict(hedge=False), dict(hedge=True, hedge_ratio=0.25), dict(hedge=True, hedge_ratio=0.5),
              dict(hedge=True, hedge_ratio=1.0)],
    "ddsize": [dict(dd_halve_at=None), dict(dd_halve_at=0.05), dict(dd_halve_at=0.10)],
    # item 0: liquidity. First row = the old flat 10% cost and 5-contract gate.
    "liq": [dict(cost_model="flat", min_entry_volume=5, min_entry_oi=0),
            dict(cost_model="spread", min_entry_volume=5, min_entry_oi=0),   # costs only
            dict(cost_model="spread", min_entry_volume=25, min_entry_oi=50),  # default gate
            dict(cost_model="spread", min_entry_volume=100, min_entry_oi=200)],
    # items 1 + 2: window, ladder, hurdle
    "window": [dict(dte_lo=21, dte_hi=35), dict(dte_lo=21, dte_hi=45), dict(dte_lo=14, dte_hi=45)],
    "ladder": [dict(ladder_entries=1), dict(ladder_entries=2, ladder_min_gap_days=2),
               dict(ladder_entries=3, ladder_min_gap_days=2), dict(ladder_entries=2, ladder_min_gap_days=4)],
    "hurdle": [dict(exit_hurdle=None, hurdle_swap=None), dict(exit_hurdle=0.07), dict(exit_hurdle=0.10),
               dict(exit_hurdle=0.07, hurdle_swap=0.12), dict(exit_hurdle=0.07, hurdle_swap=0.15)],
    # item 4: call entry filter (the trend-break exit was tested and dropped)
    "calltrend": [dict(call_trend_dist=None, call_trend_ret=None), dict(call_trend_dist=0.05),
                  dict(call_trend_dist=0.10), dict(call_trend_ret=0.08), dict(call_trend_ret=0.12)],
}


def run_grid(base: Params, variants: list, label: str, data: Optional[Data] = None) -> list:
    out, cache = [], {}
    for v in variants:
        p = replace(base, **v)
        k = data_key(p)
        if k not in cache:
            cache.clear()                    # one data set in memory at a time (a revisited key reloads)
            cache[k] = data if (data is not None and k == data_key(base)) else load_data(p)
        r = simulate(p, cache[k])
        rid = save(r, label=f"{label} {v}")
        out.append((v, rid, r.metrics))
        _print_row(v, r.metrics)
    return out


def _print_row(v, m):
    print(f"  {str(v):<52} trades={m.get('trades', 0):5d}  pnl={m.get('pnl', 0):>11,}  "
          f"cagr_cash={m.get('cagr_on_cash', 0):7.2%}  maxDD={m.get('max_dd_pct_cash', 0):7.2%}  "
          f"worstM={m.get('worst_month_rs', 0):>10,}  win={m.get('win_rate', 0):.3f}  "
          f"calmar={m.get('calmar')}  covidDD={m.get('covid_dd_pct_cash')}  "
          f"stressMax={m.get('max_stress_pct_cash')}  hedge={m.get('hedge_pnl')}  interest={m.get('interest_rs')}")


def add_param_args(ap) -> None:
    """Strategy flags shared by wheel_bt and wheel_paper (all optional; unset = Params default)."""
    ap.add_argument("--from", dest="start", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    ap.add_argument("--to", dest="end", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    ap.add_argument("--rom", type=float)
    ap.add_argument("--delta-cap", type=float)
    ap.add_argument("--entry-mode", choices=["rank_move", "all"])
    ap.add_argument("--max-new", type=int)
    ap.add_argument("--sides", choices=["both", "PE", "CE"])
    ap.add_argument("--symbols", help="comma whitelist, e.g. LICHSGFIN,CIPLA")
    ap.add_argument("--iv-source", choices=["span", "implied"], help="stress-test IV (default span)")
    ap.add_argument("--cash-yield", type=float, help="annual yield on net cash, e.g. 0.065 (default 0)")
    ap.add_argument("--cash-yield-file", help=f"dated rate table CSV (date,rate), e.g. {DEFAULT_RATE_FILE}")
    ap.add_argument("--cash-haircut", type=float, help="margin haircut on that cash, e.g. 0.10 (default 0)")
    ap.add_argument("--min-rich", type=float, help="enter only if premium / model price at SPAN vol >= this")
    ap.add_argument("--rich-rate", type=float, help="rate for the richness model price (default 0.065)")
    ap.add_argument("--price-basis", choices=["close", "settle"],
                    help="fill reference: close (default, traded) or settle (old; theoretical when untraded)")
    ap.add_argument("--universe", choices=["pit", "current"],
                    help="pit (default): index members as of each day; current: today's list throughout")
    ap.add_argument("--dte-lo", type=int)
    ap.add_argument("--dte-hi", type=int)
    ap.add_argument("--sizing", choices=["fixed", "pct"])
    ap.add_argument("--size-pct", type=float, help="pct sizing: margin per trade as fraction of capacity")
    ap.add_argument("--stop-mult", type=float)
    ap.add_argument("--no-stop-mult", action="store_true")
    ap.add_argument("--spot-cross", action="store_true")
    ap.add_argument("--stop-delta", type=float)
    ap.add_argument("--profit", type=float)
    ap.add_argument("--event-scope", choices=["board", "results"])
    ap.add_argument("--no-pit", action="store_true", help="event gate may see dates before they were announced")
    ap.add_argument("--no-coverage-gate", action="store_true")
    ap.add_argument("--util", type=float)
    ap.add_argument("--slippage", type=float)
    ap.add_argument("--cash", type=float)
    ap.add_argument("--collateral", type=float)
    # liquidity (item 0)
    ap.add_argument("--min-vol", type=int, help="contracts traded on the entry day (default 25)")
    ap.add_argument("--min-oi", type=int, help="open interest in contracts on the entry day (default 50)")
    ap.add_argument("--cost-model", choices=["spread", "flat"])
    # portfolio risk (item P)
    ap.add_argument("--stress-cap", type=float, help="book scenario loss cap as fraction of cash; 0 = off")
    ap.add_argument("--no-brake", action="store_true", help="disable the VIX / NIFTY market brake (default)")
    ap.add_argument("--brake", action="store_true", help="enable the brake at VIX 25 / jump 30%% / NIFTY -5%%")
    ap.add_argument("--rank-by", choices=["rom", "stress"], help="entry ranking for --entry-mode all")
    ap.add_argument("--stress-trim", type=float, help="trim the book while stress > this x the cap")
    ap.add_argument("--idio", help="single-stock shocks 'move:ivmult,...', e.g. 0.20:1.3 or 0.20:1.3,-0.20:1.3")
    ap.add_argument("--idio-cap", type=float, help="worst single-name shock loss cap, fraction of cash "
                                                    "(default: the stress cap)")
    ap.add_argument("--put-block-dma", type=int, choices=[50, 200],
                    help="no new puts while NIFTY is below this day average (and VIX below --put-block-vix)")
    ap.add_argument("--put-block-vix", type=float, help="put gate applies only while India VIX is below this")
    ap.add_argument("--max-iv-hv", type=float, help="enter only if ATM IV / HV20 <= this")
    ap.add_argument("--pick-by", choices=["delta", "rich", "volrich", "rom"], help="strike choice among qualifying strikes")
    ap.add_argument("--rich-measure", choices=["price", "vol"], help="richness: premium/model price or strike IV/SPAN vol")
    ap.add_argument("--min-delta", type=float, help="qualifying strikes need |delta| >= this")
    ap.add_argument("--rich-confirm", type=int, help="gate must also pass on the previous N sessions")
    ap.add_argument("--ca-scope", choices=["adjusting", "all"], help="CA blackout: contract-adjusting events only, "
                    "or every ex-date (pre-28-Oct behaviour)")
    ap.add_argument("--no-pit-ca", action="store_true", help="CA ex-dates known in advance (pre-28-Oct look-ahead)")
    ap.add_argument("--max-symbol-notional", type=float, help="per-stock open notional <= this x cash")
    ap.add_argument("--max-put-notional", type=float, help="open put notional <= this x (cash + collateral)")
    ap.add_argument("--hedge", action="store_true", help="long NIFTY put hedge (backtest only)")
    ap.add_argument("--hedge-ratio", type=float)
    ap.add_argument("--dd-halve", type=float, help="halve pct sizing while drawdown > this x cash")
    # items 1, 2, 4
    ap.add_argument("--ladder", type=int, help="entries per stock-side spread over days")
    ap.add_argument("--ladder-gap", type=int, help="trading days between ladder entries")
    ap.add_argument("--hurdle", type=float, help="hard exit when remaining yield < this a year")
    ap.add_argument("--hurdle-swap", type=float, help="soft exit below this, only to make room")
    ap.add_argument("--call-dist", type=float, help="no new calls this far above the 50-day average")
    ap.add_argument("--call-ret", type=float, help="no new calls after a 20-session return above this")


def params_from_args(a, base: Optional[Params] = None) -> Params:
    kw = {}
    for f, k in (("start", "start"), ("end", "end"), ("rom", "rom_target"), ("delta_cap", "delta_cap"),
                 ("entry_mode", "entry_mode"), ("stop_mult", "stop_mult"), ("stop_delta", "stop_delta"),
                 ("profit", "profit_residual"), ("util", "util_cap"), ("slippage", "slippage"),
                 ("max_new", "max_new_per_day"), ("dte_lo", "dte_lo"), ("dte_hi", "dte_hi"),
                 ("sizing", "sizing"), ("size_pct", "trade_margin_pct"), ("cash", "cash"),
                 ("collateral", "collateral"), ("event_scope", "event_scope"),
                 ("min_vol", "min_entry_volume"), ("min_oi", "min_entry_oi"), ("cost_model", "cost_model"),
                 ("hedge_ratio", "hedge_ratio"), ("dd_halve", "dd_halve_at"), ("ladder", "ladder_entries"),
                 ("ladder_gap", "ladder_min_gap_days"), ("hurdle", "exit_hurdle"), ("hurdle_swap", "hurdle_swap"),
                 ("call_dist", "call_trend_dist"), ("call_ret", "call_trend_ret"), ("rank_by", "rank_by"),
                 ("stress_trim", "stress_trim_at"), ("idio_cap", "stress_idio_cap"),
                 ("universe", "universe"), ("iv_source", "iv_source"),
                 ("price_basis", "price_basis"), ("min_rich", "min_rich"), ("rich_rate", "rich_rate"),
                 ("cash_yield", "cash_yield"), ("cash_haircut", "cash_haircut"),
                 ("cash_yield_file", "cash_yield_file"), ("put_block_dma", "put_block_dma"),
                 ("put_block_vix", "put_block_vix"), ("max_iv_hv", "max_iv_hv"), ("pick_by", "pick_by"),
                 ("rich_measure", "rich_measure"), ("min_delta", "min_delta"), ("rich_confirm", "rich_confirm_days"),
                 ("max_symbol_notional", "max_symbol_notional"), ("max_put_notional", "max_put_notional"),
                 ("ca_scope", "ca_scope")):
        v = getattr(a, f, None)
        if v is not None:
            kw[k] = v
    if getattr(a, "stress_cap", None) is not None:
        kw["stress_cap"] = a.stress_cap if a.stress_cap > 0 else None
    if getattr(a, "idio", None):
        kw["stress_idio"] = tuple(tuple(float(x) for x in s.split(":")) for s in a.idio.split(",") if s.strip())
    if getattr(a, "no_pit_ca", False):
        kw["pit_ca"] = False
    if getattr(a, "no_brake", False):
        kw.update(brake_vix_level=None, brake_vix_jump=None, brake_nifty_dd=None)
    if getattr(a, "brake", False):
        kw.update(BRAKE_ON)
    if getattr(a, "hedge", False):
        kw["hedge"] = True
    if getattr(a, "no_stop_mult", False):
        kw["stop_mult"] = None
    if getattr(a, "spot_cross", False):
        kw["stop_spot_cross"] = True
    if getattr(a, "no_pit", False):
        kw["pit_events"] = False
    if getattr(a, "no_coverage_gate", False):
        kw["require_event_coverage"] = False
    if getattr(a, "sides", None):
        kw["sides"] = ("PE", "CE") if a.sides == "both" else (a.sides,)
    if getattr(a, "symbols", None):
        kw["symbols"] = tuple(x.strip().upper() for x in a.symbols.split(",") if x.strip())
    return replace(base or Params(), **kw)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["run", "grid", "show", "report", "split", "flag"])
    ap.add_argument("--all", action="store_true", help="show: include flagged (defective) runs")
    ap.add_argument("--run", default="latest", help="report: run_id or label (default latest)")
    ap.add_argument("--split", dest="split_date", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
                    default=date(2023, 1, 1), help="split: first out-of-sample day")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--grid", choices=sorted(GRIDS), default="stops")
    add_param_args(ap)
    ap.add_argument("--label", default="")
    a = ap.parse_args(argv)

    if a.mode == "report":
        rid, label, metrics, tr = load_run(a.run)
        print_report(f"{label}  ({rid})", metrics, tr, top=a.top)
        return 0

    if a.mode == "flag":
        flag_runs()
        return 0

    if a.mode == "show":
        import duckdb
        con = duckdb.connect(str(WHEEL_DB), read_only=True)
        has_flags = "flags" in [r[0] for r in con.execute("DESCRIBE runs").fetchall()]
        where = "" if (a.all or not has_flags) else "WHERE flags IS NULL"
        print(con.execute(f"""
            SELECT run_id, label,
                   metrics->>'trades' trades, metrics->>'pnl' pnl, metrics->>'cagr_on_cash' cagr_cash,
                   metrics->>'max_dd_pct_cash' dd_cash, metrics->>'worst_month_rs' worst_month,
                   metrics->>'win_rate' win, metrics->>'calmar' calmar{", flags" if has_flags else ""}
            FROM runs {where} ORDER BY created DESC LIMIT 20""").df().to_string(index=False))
        if not has_flags:
            print("  (runs are not flagged yet: run `python wheel_bt.py flag`)")
        return 0

    base = params_from_args(a)

    if a.mode == "run":
        r = simulate(base, load_data(base))
        rid = save(r, label=a.label or "run")
        print_report(f"{a.label or 'run'}  ({rid})", r.metrics, r.trades, top=a.top)
        return 0
    if a.mode == "split":
        run_split(base, a.split_date, label=a.label or "split")
        return 0
    print(f"  grid '{a.grid}' on base {base.key()}")
    run_grid(base, GRIDS[a.grid], label=a.label or a.grid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
