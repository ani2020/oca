# Wheel backtest spec (26 Oct 2026)

Oct 26, 2026 · @Ani B

This tab is the current spec of `wheel_bt.py`, `wheel_paper.py` and `wheel_verify.py`. It supersedes the 1 Oct spec, kept as the last tab.

## Status and reference configuration

Phase 1 (Option A) is built and validated: short OTM puts and naked calls on index-constituent stocks, always closed before expiry, sized by exact NSE SPAN margin. The decision is not to deploy real money for now. Two paper books run daily.

| Setting | Book `w2145s` (no gate) | Book `w2145r` (gate 1.1) |
| --- | --- | --- |
| DTE window | 21–45 calendar days | 21–45 |
| Entry mode / pace | all candidates, ranked; 4 new a day | same |
| Ranking | premium per rupee of worst stress loss (`rank_by stress`) | same |
| Sizing | 5% of capacity per trade (`pct`) | same |
| Stop | 3× entry premium | same |
| Stress cap | 15% of cash | same |
| Richness gate | off | `min_rich` 1.1 |
| Universe / prices / stress IV | point-in-time / traded close / SPAN vol | same |
| Full period Oct 2019–Sep 2026 | ₹37.9L, CAGR 14.1% on cash, max DD 11.6% | ₹45.1L, 15.9%, 10.6% |
| Out-of-sample 2024–Sep 2026 | 11.3%/yr, DD 4.9% | 15.1%/yr, DD 3.9% |

Paper books since 28 Oct (all 21–45 DTE, 4 a day, 5% sizing, rank by stress, 3× stop, 15% stress cap):

| Book | Richness gate | Notional caps | Snapshot |
| --- | --- | --- | --- |
| `w2145s` | off | off | closing pull (rank 0) |
| `w2145r` | 1.1 | off | closing pull (rank 0) |
| `w2145r_cap` | 1.1 | per stock ≤ 2× cash, puts ≤ 1.5× capital | closing pull (rank 0) |
| `w2145s_pre` | off | off | last pre-close (rank 1) |
| `w2145r_pre` | 1.1 | off | last pre-close (rank 1) |
| w2145r\_capct | 1.1 | as w2145r\_cap, plus no new calls after a 20-day rise > 8% | closing pull (rank 0) |

The notional caps are adopted as a safety guard-rail (no return gain in the split test; they cut the single-stock tail from 6.6× to 2× cash).

Params defaults are still the original spec values (DTE 30–35, `rank_move`, 2 a day, 1.5× stop, fixed 1 lot). Runs set the reference through the CLI base: `--dte-lo 21 --dte-hi 45 --entry-mode all --max-new 4 --sizing pct --size-pct 0.05 --rank-by stress --stop-mult 3` (+ `--min-rich 1.1`).

Why not deploy: a BBB+-or-better NCD ladder capped at about ₹1L per issuer yields 10–11% with almost no effort. With interest on idle cash the strategy reaches roughly 20–22%/yr in the backtest, but the overfitting check and realistic-execution work still have to confirm that margin.

## Data and universe

NSE data only, Oct 2019 to Sep 2026 (covers COVID). Everything is point-in-time.

| Source | Used for |
| --- | --- |
| `bhavcopy.duckdb` `options_eod` (lake view) | traded close, volume, OI, lot |
| `span_contract` / `span_und` / `span_ingest_log` (lake views) | margin (short scan + ELM), delta, SPAN vol, exchange price every day; best tag per day (final `s` file, else latest intraday) |
| `cash_returns_adj` | CA-adjusted daily returns: beta, 50-day trend, HV20 |
| `index_eod` | NIFTY 50 and India VIX: stress betas, brake, put gate |
| `corporate_events`, `corporate_actions` | results blackout, CA blackout |
| `oc.duckdb` `index_constituents_hist` | point-in-time universe |
| `oc/data/cash_rates.csv` | dated cash-yield table (repo proxy) |

- **Universe:** NIFTY 50, BANKNIFTY and FINNIFTY members as of each day, from the monthly NSE Indices PDFs, Sep 2019 to Sep 2026 (85 months, `index_constituents_hist.py` download/parse/load). A month-end list applies from the next trading day. NIFTY sometimes has 51 names (demerger placeholders); BANKNIFTY has 14 from Dec 2025. `universe current` (today's list for every date) remains for comparison only: it inflated profit by about 40%.
- **Results blackout:** every board meeting except AGM/EGM counts as results (`event_scope board`), using only meetings already announced on the day (`pit_events`). A symbol is skipped until its event history starts (`require_event_coverage`).
- **Corporate actions:** no entry if a CA ex-date falls inside the contract's life; a held position closes the session before. The rule currently includes ordinary dividends and uses future ex-dates (see Known issues).
- **Renames bridged:** TATAMOTORS→TMPV, ZOMATO→ETERNAL, SRTRANSFIN→SHRIRAMFIN, INFRATEL→INDUSTOWER. A static sector map covers past members; unmapped names fall into OTHER.

## Entry rules

One candidate per day, symbol and side, then a portfolio pass that ranks and sizes them.

1. **Qualifying strikes (SQL):** monthly contract with `dte_lo`–`dte_hi` calendar days left; OTM against the SPAN underlying; traded that day (volume ≥ `min_entry_volume` 25, OI ≥ `min_entry_oi` 50 contracts); premium/margin (ROM) ≥ `rom_target` 2%; |delta| ≤ `delta_cap` 0.25. Margin = SPAN short scan × lot × `broker_mult` + ELM 3.5% × underlying × lot.
2. **Strike pick (`pick_by`):** `delta` = lowest |delta|, i.e. the furthest-OTM qualifying strike (default). Ties: furthest OTM, nearest expiry, raw symbol, so runs are deterministic. `rich` and `rom` exist for testing (backtest only).
3. **Event gates:** no results within `earn_entry_days` 5 trading days; no CA ex-date inside the contract's life.
4. **Optional gates (all off by default):** `min_rich` (premium ÷ Black-Scholes price at SPAN vol, rate 6.5%), `max_iv_hv` (ATM IV ÷ HV20), `put_block_dma` + `put_block_vix` (no puts below the NIFTY 50/200-day average while VIX is low), market brake (VIX level, VIX jump, NIFTY drawdown), `call_trend_dist` / `call_trend_ret` (no calls after a run-up). A candidate with no data for an active gate is skipped (fail closed).
5. **Ranking (`entry_mode all`):** by `rank_by` — `stress` = premium per rupee of the candidate's worst stress-scenario loss (reference) or `rom`. `rank_move` (puts on red days, calls on green, by |move|, with `move_cap` 4%) is the original mode.
6. **Admission:** at most `max_new_per_day`; one position per symbol and side (`max_lots_per_symbol_side`, `ladder_entries`); utilisation ≤ `util_cap` 50% of capacity; sector ≤ `sector_cap` 30% of that; the new trade must keep the book's worst stress loss within the stress cap (pct sizing shrinks lots to fit).

## Exit rules

Checked every session in this order; the first that fires closes the position at that day's exit price.

1. **Expiry** (only reached if nothing else fired).
2. **Results:** close `earn_exit_lead` 1 trading day before a results date that falls before expiry.
3. **Corporate action:** close the session before an ex-date inside the contract's life.
4. **Stop:** mark ≥ `stop_mult` × entry premium (reference 3×; `stop_requires_trade` off). Alternatives: `stop_spot_cross`, `stop_delta`.
5. **Hurdle** (off): remaining yield below `exit_hurdle`; `hurdle_swap` closes only to make room.
6. **Profit** (off): mark ≤ `profit_residual` × entry premium.
7. **Final exit:** ≤ `final_exit_td` 2 trading days left.
8. **Time exit:** ≤ `time_exit_td` 5 trading days left and |delta| > `near_money_delta` 0.15.
9. **Window end:** anything still open on the last day closes at its mark.

Assignment and the true wheel (covered calls on assigned stock) are out of scope for Phase 1.

## Sizing and portfolio risk

Capital is ₹25L cash plus ₹25L pledged collateral; capacity = collateral + cash × (1 − `cash_haircut`), compounding on.

- **Sizing:** `pct` = lots = floor(`trade_margin_pct` 5% × capacity ÷ margin per lot), capped at `max_lots_per_trade` 20 and `max_vol_frac` 20% of contracts traded that day; 1 lot only below `multi_lot_min_volume` 100. A trade whose single lot exceeds the cap is skipped. `fixed` = 1 lot.
- **Stress cap (on, 15% of cash):** every day the book is repriced under NIFTY −10/−15/−20% (IV ×1.5/1.75/2.0) and +10/+15% (IV ×1.0/1.1); each stock moves beta × NIFTY (rolling 250-session beta, clipped 0.5–2.0). A new trade must keep the worst scenario within the cap. Stress IV comes from the SPAN vol (`iv_source span`), falling back to price-implied IV, the position's last IV, then 40%. Unpriceable scenarios count as a full loss of qty × strike (fail closed).
- **Off, tested and kept off:** `stress_trim_at` (trim the book above the cap), `stress_idio` (one-name ±20% gaps), market brake, `dd_halve_at`, NIFTY put hedge, ladder entries.

## Prices, costs and cash interest

- **Fill price (`price_basis close`):** the bhavcopy CLOSE (`ClsPric`), which is traded and always inside the day's range. Exits use the close on days the contract traded, otherwise the SPAN price plus `untraded_slip` 10%. The old basis was SETTLE, which equals the SPAN theoretical price when a contract did not trade late: 1–4% of liquid contracts but 11% of the strategy's entries, at about 2× the day high.
- **Spread cost (`cost_model spread`):** each side pays a half-spread by that day's contract volume: <10: 53.5%, 10–24: 17%, 25–99: 9.5%, 100–499: 4.5%, 500+: 2%. Checked on 2026 quotes: premium-weighted, the model charges 6.0% on sells against 3.0% at the bid, and 6.1% on buy-backs against 0.5% at the ask. It is kept as a safety margin.
- **Fees:** brokerage ₹20 per order, STT, exchange charges and GST by date.
- **Interest on cash (off by default):** `cash_yield` (flat) or `cash_yield_file` (dated table; 3.35% reverse repo for May 2020–Apr 2022) accrues daily on net cash; `cash_haircut` 10% models a liquid-fund pledge.
- **Paper trader:** the same `step()` code, fed live chain quotes and filling at bid/ask; it reconciles each fill against the close (`slip_vs_close`). By default it uses each symbol's latest snapshot (the \~15:40 closing pull, not executable); --snap-rank 1 uses the one before it per symbol, the last pre-close quotes.

## Parameters

Every field of `Params`, grouped as in the code. "Ref" is the paper-book value where it differs from the default.

| Group | Parameter | Default | Ref | Meaning |
| --- | --- | --- | --- | --- |
| Window | `start` / `end` | 2019-10-01 / 2026-09-30 |  | backtest window |
| Entry | `dte_lo` / `dte_hi` | 30 / 35 | 21 / 45 | calendar days to expiry |
| Entry | `rom_target` | 0.02 |  | min premium / margin |
| Entry | `delta_cap` | 0.25 |  | max \|delta\| |
| Entry | `sides` / `symbols` | PE, CE / all |  | sides; whitelist |
| Entry | `universe` | pit |  | `pit` or `current` |
| Entry | `pick_by` | delta |  | `delta`, `rich`, `rom` |
| Entry | `entry_mode` / `move_cap` | rank\_move / 0.04 | all | ranking mode; max day move (rank\_move) |
| Entry | `max_new_per_day` | 2 | 4 | new trades a day |
| Entry | `max_lots_per_symbol_side` | 1 |  | positions per symbol-side |
| Liquidity | `min_entry_volume` / `min_entry_oi` | 25 / 50 |  | contracts traded / OI on entry day |
| Liquidity | `multi_lot_min_volume` | 100 |  | below this, 1 lot |
| Costs | `cost_model` / `spread_buckets` | spread / 53.5–2% |  | half-spread by volume |
| Costs | `max_quote_spread` | 0.20 |  | paper: skip wide quotes |
| Costs | `slippage` / `untraded_slip` | 0.10 / 0.10 |  | flat model; untraded exit extra |
| Costs | `brokerage` / `elm` / `broker_mult` | 20 / 0.035 / 1.0 |  | ₹ per order; ELM; margin multiple |
| Prices | `price_basis` | close |  | `close` or `settle` |
| Ladder | `ladder_entries` / `ladder_min_gap_days` | 1 / 2 |  | entries per symbol-side |
| Gates | `call_trend_dist` / `call_trend_ret` | off |  | no calls after run-up |
| Gates | `min_rich` / `rich_rate` | off / 0.065 | 1.1 (`w2145r`) | richness gate |
| Gates | `max_iv_hv` | off |  | ATM IV ÷ HV20 cap |
| Gates | `put_block_dma` / `put_block_vix` | off |  | put regime gate |
| Gates | `brake_vix_level` / `brake_vix_jump` / `brake_nifty_dd` / `brake_sides` | off / PE |  | market brake |
| Events | `earn_entry_days` / `earn_exit_lead` | 5 / 1 |  | results blackout |
| Events | `event_scope` / `pit_events` / `require_event_coverage` | board / on / on |  | event source rules |
| Exits | `stop_mult` / `stop_requires_trade` | 1.5 / off | 3.0 | premium-multiple stop |
| Exits | `stop_spot_cross` / `stop_delta` | off |  | alternative stops |
| Exits | `profit_residual` | off |  | early profit-taking |
| Exits | `time_exit_td` / `near_money_delta` / `final_exit_td` | 5 / 0.15 / 2 |  | time exits |
| Exits | `exit_hurdle` / `hurdle_swap` | off |  | remaining-yield exits |
| Portfolio | `cash` / `collateral` | ₹25L / ₹25L |  | capital |
| Portfolio | `util_cap` / `sector_cap` / `compound` | 0.50 / 0.30 / on |  | utilisation limits |
| Portfolio | `sizing` / `trade_margin_pct` | fixed / 0.05 | pct | lot sizing |
| Portfolio | `max_lots_per_trade` / `max_vol_frac` | 20 / 0.20 |  | size caps |
| Portfolio | `dd_halve_at` | off |  | halve size in drawdown |
| Risk | `stress_cap` / `stress_trim_at` | 0.15 / off |  | book stress limit; trim |
| Risk | `rank_by` | rom | stress | entry ranking |
| Risk | `iv_source` / `stress_moves` / `beta_bounds` | span / 5 scenarios / 0.5–2.0 |  | stress model |
| Risk | `stress_idio` / `stress_idio_cap` | off |  | one-name shocks |
| Hedge | `hedge` / `hedge_otm` / `hedge_ratio` | off / 0.10 / 0.5 |  | NIFTY put hedge |
| Hedge | `hedge_dte_lo` / `hedge_dte_hi` / `hedge_roll_td` / `hedge_slip` | 25 / 70 / 5 / 0.03 |  | hedge contract rules |
| Cash | `cash_yield` / `cash_yield_file` / `cash_haircut` | 0 / none / 0 |  | interest on cash |
| Gates | rich\_measure / rich\_confirm\_days | price / 0 |  | price ratio or vol ratio (strike IV ÷ SPAN vol); previous sessions that must also pass (backtest only) |
| Entry | min\_delta | off |  | delta band floor for qualifying strikes; pick\_by also takes volrich (highest vol ratio) |

## Grids and commands

Grid rows apply on top of the CLI base; each grid sweeps one dimension. Grids that change the SQL pull (`rom`, `delta`, `dte`, `window`, `pick`, `overfit`, `liq`, `universe`, `pxbasis`) reload data per value, one data set in memory at a time.

| Grid | Rows | Sweeps |
| --- | --- | --- |
| `stops` | 8 | stop multiple, spot-cross, delta stops |
| `rom` / `delta` | 4 / 4 | ROM target 1.5–3%; delta cap 0.15–0.30 |
| `sides` | 3 | both, puts only, calls only |
| `dte` / `window` | 3 / 3 | 30–35, 25–35, 21–35 / 21–35, 21–45, 14–45 |
| `pace` / `entry` | 4 / 4 | new trades a day; entry mode |
| `sizing` / `util` | 4 / 4 | fixed vs 5–10%; utilisation cap |
| `profit` | 5 | profit-taking at 50/65/80/90% kept |
| `risk` / `trim` / `rank` | 6 / 4 / 2 | stress cap and brake; trim; ranking |
| `idio` | 5 | one-name shock scenarios |
| `hedge` / `ddsize` | 4 / 3 | NIFTY put hedge; drawdown halving |
| `liq` | 4 | flat vs spread costs, liquidity gates |
| `ladder` / `hurdle` / `calltrend` | 4 / 5 / 5 | ladders; hurdle exits; call trend filters |
| `universe` / `ivsrc` / `pxbasis` | 2 / 2 / 2 | pit vs current; SPAN vs implied IV; close vs settle |
| `cashyield` | 4 | none, 6.5%, 6.5% + haircut, dated table + haircut |
| `rich` | 5 | `min_rich` off, 1.0, 1.1, 1.25, 1.5 |
| `putregime` | 4 | put gate off / 50-DMA / 50-DMA + VIX<20 / 200-DMA + VIX<20 |
| `ivhv` | 4 | `max_iv_hv` off, 1.25, 1.0, 0.8 |
| `pick` | 3 | strike pick delta / rich / rom |
| `overfit` | 24 | DTE {21–35, 21–45} × `min_rich` {off, 1.0, 1.1, 1.25} × stop {2, 3, none} |
| richsel / confirm | 8 / 3 | price vs vol-ratio gate, delta floor, richest strike in band, ROM floor 1.5–2.5% / confirmation 0, 1, 2 sessions |

| Tool | Command | Does |
| --- | --- | --- |
| backtest | `python wheel_bt.py run \| grid \| split \| show \| report \| flag` | one run; a grid; IS/OOS split with fresh capital (`--split 2024-01-01`); compare runs (`--all` shows flagged); per-stock report; flag defective runs |
| verifier | `python wheel_verify.py --run <label or id>` | rebuilds every trade from raw data without importing `wheel_bt`; checks E1–E10 (entry), X1–X2 (exit), M1–M3 (money); writes `wheel_verify_<run>.csv` |
| paper | `python wheel_paper.py` | daily paper books on live chain snapshots; status and fill reconciliation |
| signals | `python wheel_signals.py` | path-independent candidate outcomes and signal ICs |
| events | `python iv_events.py` | IV paths and straddle P&L around results and macro events |
| universe | `python index_constituents_hist.py download \| parse \| load` | builds `index_constituents_hist` |

All runs land in `wheel_bt.duckdb` (`runs` with params, metrics and flags; `trades` with `rich` and `iv_hv`; `equity` daily). Tests: `tests/test_wheel_bt.py` (35 sections), `test_wheel_paper.py`, `test_wheel_signals.py`, `test_iv_events.py`.

## Known issues and pending fixes

To be fixed together, so the reference numbers move once. None changes a conclusion so far.

| Issue | Effect | Fix |
| --- | --- | --- |
| Results date on a non-trading day | exits one session early (\~56 trades per run) | map the event to the next session |
| Calendar truncated at `end` | \~20 positions near the window end get false final/time/results exits; also at the end of a split's IS half | extend the calendar past open expiries |
| SPAN row missing on a day the contract traded | treated as untraded, exits at a stale mark (TATAMOTORS CE 110, 12 Jun 2020: ₹1.25 vs traded ₹5.30, about ₹20k) | fall back to the traded close |
| CA rule includes ordinary dividends and future ex-dates | blocks \~16% of candidates; look-ahead | price-adjusting CAs only, point-in-time (deferred: would change every result) |
| Params defaults are the original spec | CLI base needed for the reference config | consider making the reference the default |
| Paper fills at the 15:40 snapshot | not executable after the close | done 27 Oct: --snap-rank 1 (per-symbol pre-close snapshot); run on copies of the books to compare |
| `oc.index_constituents` (today's list) stale | still has WIPRO; BSE replaced it on 30 Sep 2026 | user to update |
| `cash_rates.csv` ends 2025-06-06 at 5.50% | later rate moves missing | user to append; also verify four `macro_events.csv` dates |
