"""
icici_b.py
----------
ICICI Breeze API wrapper for NSE options data and margin calculation.

Credentials are loaded from (in priority order):
  1. Environment variables already set in the shell
  2. A .env file in the current working directory (or path passed to load_dotenv)

Required .env keys:
    IC_API_KEY      = your_api_key
    IC_API_SECRET   = your_api_secret
    IC_SESS_TOKEN   = your_session_token   (refreshed daily — see print_login_url)

Session token workflow:
    1. Call ICB.print_login_url() to get the ICICI login URL.
    2. Log in via browser, copy the session_token from the redirect URL.
    3. Update IC_SESS_TOKEN in .env (or pass directly to ICB(session_token=...)).
    4. Construct ICB() — it reads the token automatically.

Usage:
    from icici_b import ICB
    icb = ICB()                                    # reads from .env / env vars
    icb = ICB(session_token="abc123")              # override token only

    margin = icb.get_margin_for_option(
        symbol="NIFTY", strike="24000", expiry=date(2025,5,29),
        option_type="call", ltp=120.5, qty=75
    )
    available = icb.get_available_margin()

CLI:
    python icici_b.py login-url              # print ICICI login URL
    python icici_b.py margin --symbol NIFTY --strike 24000 --expiry 2025-05-29
                             --type call --ltp 120.5 --qty 75
    python icici_b.py available-margin
"""

from __future__ import annotations

import argparse
import os
import traceback
import urllib.parse
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

# ---------------------------------------------------------------------------
# .env loading — optional but recommended
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv()          # loads .env from CWD; harmless if file absent
    _HAS_DOTENV = True
except ImportError:
    _HAS_DOTENV = False

# ---------------------------------------------------------------------------
# BreezeConnect — optional (raises clear error if missing)
# ---------------------------------------------------------------------------
try:
    from breeze_connect import BreezeConnect
    _HAS_BREEZE = True
except ImportError:
    _HAS_BREEZE = False


# ===========================================================================
# Constants
# ===========================================================================

_BREEZE_LOGIN_BASE = "https://api.icicidirect.com/apiuser/login"
_DATE_COL   = "%Y-%m-%d"
_DATE_DISP  = "%d-%b-%Y"


# ===========================================================================
# ICB — ICICI Breeze wrapper
# ===========================================================================

class ICB:
    """
    Thin, reusable wrapper around BreezeConnect for NSE options work.

    All credentials are read from environment variables (populated from .env
    if python-dotenv is installed). A session_token kwarg overrides the env
    variable at construction time — useful for daily token refresh without
    restarting the process.
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, session_token: Optional[str] = None) -> None:
        if not _HAS_BREEZE:
            raise ImportError(
                "breeze_connect is not installed. "
                "Run: pip install breeze-connect"
            )

        api_key    = os.environ.get("IC_API_KEY", "").strip().strip('"')
        api_secret = os.environ.get("IC_API_SECRET", "").strip().strip('"')
        token      = (session_token or os.environ.get("IC_SESS_TOKEN", "")).strip().strip('"')

        missing = [k for k, v in [("IC_API_KEY", api_key), ("IC_API_SECRET", api_secret), ("IC_SESS_TOKEN", token)] if not v]
        if missing:
            raise RuntimeError(
                f"Missing credentials: {', '.join(missing)}. "
                "Set them in .env or as environment variables."
            )

        self._breeze = BreezeConnect(api_key=api_key)
        self._breeze.generate_session(api_secret=api_secret, session_token=token)
        self._api_key = api_key

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    @staticmethod
    def print_login_url(api_key: Optional[str] = None) -> str:
        """
        Build and print the ICICI Breeze login URL.

        After logging in via the browser, copy the `apisession` value
        from the redirect URL — that is your new IC_SESS_TOKEN.

        Parameters
        ----------
        api_key : Override the key from the environment (useful in scripts
                  before ICB is constructed).

        Returns
        -------
        The URL string (also printed to stdout).
        """
        key = api_key or os.environ.get("IC_API_KEY", "").strip().strip('"')
        if not key:
            raise RuntimeError("IC_API_KEY not set — cannot build login URL.")
        url = f"{_BREEZE_LOGIN_BASE}?api_key={urllib.parse.quote(key)}"
        print(f"\n  ICICI Breeze login URL:\n  {url}\n")
        print("  Steps:")
        print("  1. Open the URL above in your browser and log in.")
        print("  2. After login, copy the 'apisession' value from the redirect URL.")
        print("  3. Set  IC_SESS_TOKEN=<that value>  in your .env file.")
        print("  4. Re-construct ICB() or call icb.refresh_session(new_token).\n")
        return url

    def refresh_session(self, session_token: str) -> None:
        """
        Update the session token without re-constructing the object.
        Useful for intraday token refresh in long-running processes.
        """
        api_secret = os.environ.get("IC_API_SECRET", "").strip().strip('"')
        self._breeze.generate_session(api_secret=api_secret, session_token=session_token.strip())
        # Also update env so child processes / re-reads pick it up
        os.environ["IC_SESS_TOKEN"] = session_token.strip()
        print("✓ Session refreshed.")

    # ------------------------------------------------------------------
    # Margin
    # ------------------------------------------------------------------

    def get_margin_for_option(
        self,
        symbol:      str,
        strike:      str,
        expiry:      date,
        option_type: str,          # "call" or "put"
        ltp:         float,
        qty:         int,
        action:      str = "sell",
        product:     str = "options",
    ) -> Optional[Dict]:
        """
        Calculate required margin for a single options order.

        Parameters
        ----------
        symbol      : NSE symbol, e.g. "NIFTY", "RELIANCE"
        strike      : Strike price as string, e.g. "24000"
        expiry      : Expiry date object
        option_type : "call" or "put"
        ltp         : Last traded price of the option
        qty         : Quantity (number of units, not lots)
        action      : "sell" (default) or "buy"
        product     : "options" (default)

        Returns
        -------
        Dict with keys:
            isec_margin  : float — margin required (₹)
            cash_limit   : float — available cash limit
            raw          : dict  — full API response
        Or None on failure.
        """
        try:
            # Breeze requires expiry as "27-Feb-2025" (DD-Mon-YYYY)
            breeze_expiry = expiry.strftime("%d-%b-%Y")   # e.g. "27-Feb-2025"
            # Breeze requires right as "Call" or "Put" (capitalised)
            breeze_right = "Call" if option_type.lower() == "call" else "Put"
            payload = [{
                "strike_price":     str(strike),
                "quantity":         str(qty),
                "right":            breeze_right,
                "product":          product,
                "action":           action.lower(),
                "price":            str(ltp),
                "expiry_date":      breeze_expiry,
                "stock_code":       symbol.upper(),
                "cover_order_flow": "N",
                "fresh_order_type": "N",
                "cover_limit_rate": "0",
                "cover_sltp_price": "0",
                "fresh_limit_rate": "0",
                "open_quantity":    "0",
            }]
            resp = self._breeze.margin_calculator(payload, exchange_code="NFO")
            return self._parse_margin_response(resp)

        except Exception as exc:
            print(f"✗ get_margin_for_option failed: {exc}")
            traceback.print_exc()
            return None

    def get_available_margin(self) -> Optional[Dict]:
        """
        Fetch available margin from the NFO exchange.

        Returns
        -------
        Dict with keys:
            cash_limit       : float
            amount_allocated : float
            block_by_trade   : float
            isec_margin      : float
            raw              : dict
        Or None on failure.
        """
        try:
            resp = self._breeze.get_margin(exchange_code="NFO")
            return self._parse_margin_response(resp)
        except Exception as exc:
            print(f"✗ get_available_margin failed: {exc}")
            traceback.print_exc()
            return None

    @staticmethod
    def _parse_margin_response(resp: dict) -> Optional[Dict]:
        """
        Parse a Breeze margin API response.

        margin_calculator  → Success is a LIST of position dicts,
                             each has "margin_amount" (per-leg margin).
                             We sum all legs to get total required margin.
        get_margin         → Success is a DICT with isec_margin / cash_limit.
        """
        if resp is None or resp.get("Status") != 200:
            err = resp.get("Error") if resp else "None response"
            print(f"✗ Margin API error: {err}")
            return None

        success = resp.get("Success")

        def _f(d, key: str) -> float:
            try:
                return float(d.get(key) or 0)
            except (TypeError, ValueError):
                return 0.0

        # margin_calculator returns a dict with span/order margin totals
        # and a "margin_calulation" list of per-leg echo.
        # Response structure:
        #   Success.span_margin_required  — total SPAN margin (main figure)
        #   Success.order_margin          — exchange margin (often 0 for options sell)
        #   Success.order_value           — notional order value
        if isinstance(success, dict) and "span_margin_required" in success:
            span   = _f(success, "span_margin_required")
            order  = _f(success, "order_margin")
            # Total margin = SPAN + any additional order margin
            total  = span + order if order > 0 else span
            return {
                "isec_margin":          total,
                "span_margin":          span,
                "order_margin":         order,
                "order_value":          _f(success, "order_value"),
                "block_trade_margin":   _f(success, "block_trade_margin"),
                "cash_limit":           0.0,
                "amount_allocated":     0.0,
                "block_by_trade":       0.0,
                "raw":                  resp,
            }

        # get_margin returns a flat dict
        if isinstance(success, dict):
            return {
                "isec_margin":      _f(success, "isec_margin"),
                "cash_limit":       _f(success, "cash_limit"),
                "amount_allocated": _f(success, "amount_allocated"),
                "block_by_trade":   _f(success, "block_by_trade"),
                "raw":              resp,
            }

        print(f"✗ Unexpected margin response structure: {type(success)}")
        return None

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def cancel_order(self, exchange_code: str, order_id: str) -> Optional[Dict]:
        """Cancel an open order. Returns response dict or None."""
        try:
            resp = self._breeze.cancel_order(
                exchange_code=exchange_code, order_id=order_id
            )
            if resp and resp.get("Status") == 200:
                return resp
            print(f"✗ cancel_order: {resp.get('Error') if resp else 'no response'}")
            return None
        except Exception as exc:
            print(f"✗ cancel_order exception: {exc}")
            traceback.print_exc()
            return None

    def get_order_list(
        self,
        exchange_code: str,
        from_date: date,
        to_date: date,
    ) -> Optional[List[Dict]]:
        """Return list of orders for the given date range."""
        try:
            resp = self._breeze.get_order_list(exchange_code, from_date, to_date)
            if resp and resp.get("Status") == 200:
                return resp.get("Success", [])
            return None
        except Exception as exc:
            print(f"✗ get_order_list: {exc}")
            return None

    # ------------------------------------------------------------------
    # Historical data
    # ------------------------------------------------------------------

    def get_intraday_historical(
        self,
        symbol:    str,
        sec_type:  str,          # "EQ" or "FU"
        from_date: date,
        to_date:   Optional[date] = None,
        interval:  str = "1minute",
    ) -> Optional[List[Dict]]:
        """
        Fetch intraday historical data from ICICI Breeze.

        Parameters
        ----------
        symbol   : NSE ticker
        sec_type : "EQ" for equity cash, "FU" for futures
        from_date, to_date : Date range (to_date defaults to today)
        interval : Candle interval, e.g. "1minute", "5minute", "1day"

        Returns
        -------
        List of OHLCV dicts, or None on failure.
        """
        if not to_date:
            to_date = date.today()
        if to_date < from_date:
            raise ValueError("from_date must be <= to_date")

        type_map = {
            "EQ": ("NSE", "cash"),
            "FU": ("NFO", "futures"),
        }
        if sec_type not in type_map:
            raise ValueError(f"sec_type must be 'EQ' or 'FU', got {sec_type!r}")
        ex_code, prod_type = type_map[sec_type]

        chunks = ICB.split_date_range(from_date, to_date, chunk_days=2)
        rows: List[Dict] = []

        for start, end in chunks:
            f_str   = f"{start.strftime(_DATE_COL)}T09:15:00.000Z"
            t_str   = f"{end.strftime(_DATE_COL)}T15:30:00.000Z"
            ex_date = ICB.get_monthly_expiry(start).strftime(_DATE_COL)
            print(f"  Fetching {symbol} {f_str} → {t_str}")
            try:
                resp = self._breeze.get_historical_data_v2(
                    interval=interval,
                    from_date=f_str,
                    to_date=t_str,
                    expiry_date=ex_date,
                    stock_code=symbol,
                    exchange_code=ex_code,
                    product_type=prod_type,
                )
                if resp and resp.get("Status") == 200:
                    rows.extend(resp.get("Success", []))
                else:
                    print(f"  ✗ {resp.get('Error') if resp else 'None'}")
            except Exception as exc:
                print(f"  ✗ chunk error: {exc}")

        return rows if rows else None

    def get_option_chain_quotes(
        self,
        symbol:      str,
        expiry:      date,
        option_type: str = "call",   # "call" or "put"
    ) -> Optional[List[Dict]]:
        """
        Fetch live option chain quotes from ICICI Breeze for one expiry.
        """
        try:
            resp = self._breeze.get_option_chain_quotes(
                stock_code=symbol.upper(),
                exchange_code="NFO",
                product_type="options",
                right=option_type.lower(),
                expiry_date=expiry,
            )
            if resp and resp.get("Status") == 200:
                return resp.get("Success", [])
            print(f"✗ get_option_chain_quotes: {resp.get('Error') if resp else 'None'}")
            return None
        except Exception as exc:
            print(f"✗ get_option_chain_quotes: {exc}")
            traceback.print_exc()
            return None

    # ------------------------------------------------------------------
    # Static date utilities
    # ------------------------------------------------------------------

    @staticmethod
    def get_monthly_expiry(input_date: date) -> date:
        """
        Last Thursday (pre Aug-2025) or last Tuesday (Aug-2025+) of the month.
        Rolls to next month if input_date is already past this month's expiry.
        """
        if isinstance(input_date, str):
            input_date = datetime.strptime(input_date, "%Y-%m-%d").date()
        elif isinstance(input_date, datetime):
            input_date = input_date.date()

        transition = date(2025, 8, 1)
        target_wd  = 1 if input_date >= transition else 3   # Tue=1, Thu=3

        year, month   = input_date.year, input_date.month
        last_day      = monthrange(year, month)[1]
        last_date     = date(year, month, last_day)
        days_back     = (last_date.weekday() - target_wd) % 7
        expiry        = date(year, month, last_day - days_back)

        if expiry < input_date:
            # Roll to next month
            expiry = ICB.get_monthly_expiry(input_date + timedelta(days=7))
        return expiry

    @staticmethod
    def split_date_range(
        from_date: date, to_date: date, chunk_days: int = 365
    ) -> List[Tuple[date, date]]:
        """Split a date range into non-overlapping chunks of at most chunk_days."""
        chunks, cur = [], from_date
        while cur <= to_date:
            end = min(cur + timedelta(days=chunk_days - 1), to_date)
            chunks.append((cur, end))
            cur = end + timedelta(days=1)
        return chunks


# ===========================================================================
# CLI
# ===========================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="icici_b.py",
        description="ICICI Breeze API wrapper — margin, historical data, order management.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # login-url
    sub.add_parser("login-url", help="Print the ICICI Breeze login URL to get a session token")

    # margin
    pm = sub.add_parser("margin", help="Calculate margin for a single options order")
    pm.add_argument("--symbol",  required=True)
    pm.add_argument("--strike",  required=True)
    pm.add_argument("--expiry",  required=True, help="YYYY-MM-DD")
    pm.add_argument("--type",    required=True, choices=["call","put"])
    pm.add_argument("--ltp",     required=True, type=float)
    pm.add_argument("--qty",     required=True, type=int)
    pm.add_argument("--action",  default="sell", choices=["buy","sell"])

    # available-margin
    sub.add_parser("available-margin", help="Show available margin on NFO")

    args = parser.parse_args()

    if args.cmd == "login-url":
        ICB.print_login_url()
        return

    icb = ICB()

    if args.cmd == "margin":
        expiry = datetime.strptime(args.expiry, "%Y-%m-%d").date()
        result = icb.get_margin_for_option(
            symbol=args.symbol, strike=args.strike, expiry=expiry,
            option_type=args.type, ltp=args.ltp, qty=args.qty, action=args.action,
        )
        if result:
            print(f"  Required margin  : ₹{result['isec_margin']:,.2f}")
            print(f"  Cash limit       : ₹{result['cash_limit']:,.2f}")
        else:
            print("✗ Could not fetch margin")

    elif args.cmd == "available-margin":
        result = icb.get_available_margin()
        if result:
            print(f"  Cash limit       : ₹{result['cash_limit']:,.2f}")
            print(f"  Amount allocated : ₹{result['amount_allocated']:,.2f}")
            print(f"  Blocked by trade : ₹{result['block_by_trade']:,.2f}")
        else:
            print("✗ Could not fetch margin")


if __name__ == "__main__":
    main()
