-- =============================================================================
-- Exposure Screener — table schemas (Phase A)
-- Run once against oc.duckdb. Safe to re-run (IF NOT EXISTS).
-- =============================================================================

-- ── Main EOD exposure metrics, one row per (symbol, date, expiry_rank) ─────
-- expiry_rank: 0 = NEAR (front monthly, DTE > DTE_EXCLUDE), 1 = NEXT monthly.
CREATE TABLE IF NOT EXISTS exposure_eod (
    symbol                  VARCHAR   NOT NULL,
    date                    DATE      NOT NULL,
    expiry                  DATE,                 -- monthly expiry used
    expiry_rank             TINYINT   NOT NULL DEFAULT 0,  -- 0=NEAR, 1=NEXT
    dte                     INTEGER,
    fut_price               DOUBLE,               -- range center (forward)
    spot                    DOUBLE,
    expected_move           DOUBLE,               -- smoothed-IV based
    strike_interval         DOUBLE,
    range_lo                DOUBLE,
    range_hi                DOUBLE,

    -- ATM IV
    atm_iv                  DOUBLE,               -- spot ATM IV from DB
    atm_iv_smoothed         DOUBLE,               -- 5-day EWMA (stored for trace)
    iv_change               DOUBLE,               -- vs prev day atm_iv
    basis                   DOUBLE,               -- fut_price - spot (NULL if fut==spot fallback)
    basis_pct               DOUBLE,               -- basis / spot * 100
    basis_annualized        DOUBLE,               -- basis_pct * 365/dte (clamped; NULL if distorted)
    basis_chg               DOUBLE,               -- Δbasis_annualized vs prev session (Stage 2)

    -- Gamma
    gamma_flip              DOUBLE,               -- interpolated zero-crossing (NULL if none)
    flip_nearest            DOUBLE,               -- nearest real strike
    flip_velocity           DOUBLE,               -- Δflip / trading-day
    flip_norm_distance      DOUBLE,               -- (flip - fut) / expected_move
    net_gex                 DOUBLE,               -- Σ net_gexv in range (raw, ₹M)
    net_gex_sign            VARCHAR,              -- aggregate sign (vs at-spot regime)
    net_gex_norm            DOUBLE,               -- net_gex / total OI in range (comparable)
    gex_regime              VARCHAR,              -- AT-SPOT regime (local pinning/amplify)              -- positive | negative | all_positive |
                                                  --   all_negative | flip_beyond_range
    transition_width        DOUBLE,               -- last +γ strike → first -γ strike
    transition_width_norm   DOUBLE,               -- / expected_move
    neg_gamma_fraction      DOUBLE,               -- fraction of range in -γ
    peak_pos_gamma_strike   DOUBLE,
    peak_neg_gamma_strike   DOUBLE,

    -- Vanna (CE/PE kept separate — never netted for crash signal)
    ce_vanna                DOUBLE,
    pe_vanna                DOUBLE,
    net_vanna               DOUBLE,               -- reference only
    net_charm               DOUBLE,

    -- Quality / context
    total_oi_in_range       DOUBLE,
    oi_turnover_ratio       DOUBLE,                -- Σ|oi_change|/Σoi (indicator)
    n_strikes               INTEGER,
    confidence              VARCHAR,              -- high | medium | low
    near_earnings           BOOLEAN DEFAULT FALSE,

    -- Forward outcomes (filled next day, one-day lag)
    next_day_realized_move  DOUBLE,               -- signed % move of fut/spot
    next_day_realized_range DOUBLE,               -- high-low range %
    next_day_abs_move       DOUBLE,               -- |move| for hit-rate

    -- Signals (categorical; fired on this date's transition)
    signals                 VARCHAR,              -- comma-separated fired signals (Stage 2)
    active_regime           VARCHAR,              -- current standing regime label
    -- Stage-2 derived temporal metrics
    regime_compression      BOOLEAN DEFAULT FALSE,-- coiling-spring state (indicator)
    compression_days        INTEGER,              -- consecutive sessions compressing
    compression_release     BOOLEAN DEFAULT FALSE,-- break-into-expansion event (indicator)
    days_in_regime          INTEGER,              -- consecutive sessions in gex_regime
    days_since_flip         INTEGER,              -- sessions since last regime change
    days_since_release      INTEGER,

    computed_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, date, expiry_rank)
);

-- ── Manual events table (earnings / dividends / corporate actions) ─────────
CREATE TABLE IF NOT EXISTS ticker_events (
    id          INTEGER,
    symbol      VARCHAR   NOT NULL,
    event_date  DATE      NOT NULL,
    event_type  VARCHAR   NOT NULL,   -- earnings | dividend | corporate_action | other
    notes       VARCHAR,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── Manual lot-size overrides (NSE changes lot sizes periodically) ─────────
CREATE TABLE IF NOT EXISTS lot_size_overrides (
    symbol        VARCHAR   NOT NULL,
    lot_size      INTEGER   NOT NULL,
    effective_from DATE,
    notes         VARCHAR,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol)
);

-- ── Index constituents mapping (a symbol can be in multiple indices) ───────
CREATE TABLE IF NOT EXISTS index_constituents (
    index_name  VARCHAR   NOT NULL,   -- NIFTY | BANKNIFTY | FINNIFTY | ...
    symbol      VARCHAR   NOT NULL,
    PRIMARY KEY (index_name, symbol)
);

-- For existing tables, add the new columns:
ALTER TABLE exposure_eod ADD COLUMN IF NOT EXISTS net_gex_sign VARCHAR;
ALTER TABLE exposure_eod ADD COLUMN IF NOT EXISTS net_gex_norm DOUBLE;
ALTER TABLE exposure_eod ADD COLUMN IF NOT EXISTS oi_turnover_ratio DOUBLE;
ALTER TABLE exposure_eod ADD COLUMN IF NOT EXISTS regime_compression BOOLEAN;
ALTER TABLE exposure_eod ADD COLUMN IF NOT EXISTS compression_days INTEGER;
ALTER TABLE exposure_eod ADD COLUMN IF NOT EXISTS compression_release BOOLEAN;
ALTER TABLE exposure_eod ADD COLUMN IF NOT EXISTS days_in_regime INTEGER;
ALTER TABLE exposure_eod ADD COLUMN IF NOT EXISTS days_since_flip INTEGER;
ALTER TABLE exposure_eod ADD COLUMN IF NOT EXISTS days_since_release INTEGER;
ALTER TABLE exposure_eod ADD COLUMN IF NOT EXISTS basis DOUBLE;
ALTER TABLE exposure_eod ADD COLUMN IF NOT EXISTS basis_pct DOUBLE;
ALTER TABLE exposure_eod ADD COLUMN IF NOT EXISTS basis_annualized DOUBLE;
ALTER TABLE exposure_eod ADD COLUMN IF NOT EXISTS basis_chg DOUBLE;
-- v3 (near+next expiry): tag every existing row as NEAR (rank 0). Existing
-- history predates the two-expiry change and is all front-monthly.
ALTER TABLE exposure_eod ADD COLUMN IF NOT EXISTS expiry_rank TINYINT DEFAULT 0;
UPDATE exposure_eod SET expiry_rank = 0 WHERE expiry_rank IS NULL;

-- ── PK rebuild: (symbol, date) → (symbol, date, expiry_rank) ───────────────
-- DuckDB cannot alter a primary key in place, so rebuild the table. This block
-- is only needed ONCE on an existing DB whose PK is still (symbol, date). It is
-- safe to SKIP entirely if you are dropping + recreating the table for a clean
-- re-backfill (the CREATE above already has the correct 3-col PK).
--
-- To run the in-place migration, uncomment the block below. It preserves all
-- existing rows (tagged rank 0). Verify row counts match before/after.
--
-- CREATE TABLE exposure_eod_new AS SELECT * FROM exposure_eod;  -- carries data, no PK
-- -- recreate with PK by selecting into a freshly-defined table:
-- BEGIN;
--   ALTER TABLE exposure_eod RENAME TO exposure_eod_old;
--   -- (re-run the CREATE TABLE IF NOT EXISTS exposure_eod (...) above here)
--   INSERT INTO exposure_eod SELECT * FROM exposure_eod_old;
--   DROP TABLE exposure_eod_old;
-- COMMIT;
--
-- RECOMMENDED for this change: since a full re-backfill is planned, instead just
--   DROP TABLE IF EXISTS exposure_eod;
-- then run this whole script (clean CREATE with the 3-col PK), then run
--   python oc_exposure_eod.py --db oc.duckdb --backfill

-- Verify
SELECT 'exposure_eod' AS tbl, COUNT(*) AS rows FROM exposure_eod
UNION ALL SELECT 'ticker_events', COUNT(*) FROM ticker_events
UNION ALL SELECT 'lot_size_overrides', COUNT(*) FROM lot_size_overrides
UNION ALL SELECT 'index_constituents', COUNT(*) FROM index_constituents;
