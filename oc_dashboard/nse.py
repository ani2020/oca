"""NSEFetcher singleton — one instance per app lifetime."""
from __future__ import annotations
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException

from . import config

_nse_fetcher_instance: Optional[Any] = None


def init_nse_fetcher() -> None:
    """Warm up the shared NSEFetcher at startup. Silent on failure."""
    global _nse_fetcher_instance
    try:
        _nse_root = Path(config.DB_FILE).resolve().parent
        if str(_nse_root) not in sys.path:
            sys.path.insert(0, str(_nse_root))
        from nse_fetcher import NSEFetcher
        _cookies_path = str(_nse_root / "nse_cookies.json")
        _nse_fetcher_instance = NSEFetcher(cookies_file=_cookies_path)
        _ = _nse_fetcher_instance._get_session()
        print("✓ NSEFetcher session ready")
    except Exception as exc:
        print(f"  ℹ NSEFetcher not available at startup: {exc}")
        _nse_fetcher_instance = None


def get_fetcher():
    """Return the shared NSEFetcher singleton. Raises HTTPException if unavailable."""
    global _nse_fetcher_instance
    if _nse_fetcher_instance is not None:
        return _nse_fetcher_instance
    try:
        _nse_root = Path(config.DB_FILE).resolve().parent
        if str(_nse_root) not in sys.path:
            sys.path.insert(0, str(_nse_root))
        from nse_fetcher import NSEFetcher
        _cookies_path = str(_nse_root / "nse_cookies.json")
        _nse_fetcher_instance = NSEFetcher(cookies_file=_cookies_path)
        return _nse_fetcher_instance
    except ModuleNotFoundError:
        raise HTTPException(503, "NSEFetcher not found")
    except Exception as exc:
        raise HTTPException(502, f"NSEFetcher init failed: {exc}")
