"""Shared configuration for the OC Dashboard."""
from __future__ import annotations
from pathlib import Path

DB_FILE  = "oc.duckdb"
DB_TABLE = "ocdata"

NSE_INDICES = {
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
    "NIFTYNXT50", "SENSEX", "NIFTYIT",
}

# ICICI Breeze ticker map — loaded once at startup
ICICI_MAP: dict = {}

# Safe metrics whitelist for strike_trend
SAFE_METRICS = {
    "ce_ltp", "pe_ltp", "ce_oi", "pe_oi", "ce_oi_change", "pe_oi_change",
    "ce_iv", "pe_iv", "ce_delta", "pe_delta", "ce_gamma", "pe_gamma",
    "ce_theta", "pe_theta", "ce_vega", "pe_vega", "ce_volume", "pe_volume",
    "ce_rho", "pe_rho", "ce_vanna", "pe_vanna", "ce_charm", "pe_charm",
    "underlying_price", "ce_TPrice", "pe_TPrice", "ce_ltp_s", "pe_ltp_s",
    "ce_intrinsic_value", "pe_intrinsic_value", "ce_time_value", "pe_time_value",
    "ce_gexv", "pe_gexv", "net_gexv",
    "ce_nd1", "ce_nd2", "pe_nd1", "pe_nd2",
    "ce_prem_oi", "pe_prem_oi", "ce_prem_oi_chg", "pe_prem_oi_chg",
    "ce_delta_oi", "pe_delta_oi", "ce_delta_oi_chg", "pe_delta_oi_chg",
    "expected_move_theoretical", "expected_move_straddle",
}
