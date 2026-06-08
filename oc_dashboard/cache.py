"""Caching — simple in-memory cache + margin cache with file persistence."""
from __future__ import annotations
import json
import pickle
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from . import config

# ═══════════════════════════════════════════════════════════════════
# Simple in-memory cache (symbols, expiries, lot sizes, overview)
# ═══════════════════════════════════════════════════════════════════
_CACHE: Dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()


def cache_get(key: str) -> Optional[Any]:
    with _CACHE_LOCK:
        return _CACHE.get(key)


def cache_set(key: str, value: Any) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = value


def cache_clear_all() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


# ═══════════════════════════════════════════════════════════════════
# Margin calculation cache — persisted to disk
# ═══════════════════════════════════════════════════════════════════
_MARGIN_CACHE: Dict[str, Dict] = {}
_MARGIN_LOCK = threading.Lock()


def _margin_cache_path() -> Path:
    """Path next to the DB file for margin cache persistence."""
    return Path(config.DB_FILE).with_suffix(".margin_cache.pkl")


def margin_cache_load() -> None:
    global _MARGIN_CACHE
    p = _margin_cache_path()
    if not p.exists():
        return
    try:
        with open(p, "rb") as f:
            _MARGIN_CACHE = pickle.load(f)
        print(f"  ✓ Margin cache loaded ({len(_MARGIN_CACHE)} entries)")
    except Exception as exc:
        print(f"  ℹ Margin cache load failed: {exc}")
        _MARGIN_CACHE = {}


def margin_cache_save(verbose: bool = False) -> None:
    p = _margin_cache_path()
    try:
        with _MARGIN_LOCK:
            with open(p, "wb") as f:
                pickle.dump(_MARGIN_CACHE, f)
        if verbose:
            print(f"  ✓ Margin cache saved ({len(_MARGIN_CACHE)} entries)")
    except Exception as exc:
        if verbose:
            print(f"  ℹ Margin cache save failed: {exc}")


def margin_cache_get(symbol: str, strike: str, expiry: str, option_type: str) -> Optional[Dict]:
    key = f"{symbol}|{strike}|{expiry}|{option_type}"
    with _MARGIN_LOCK:
        return _MARGIN_CACHE.get(key)


def margin_cache_put(symbol: str, strike: str, expiry: str, option_type: str,
                     data: Dict, margin: float) -> None:
    key = f"{symbol}|{strike}|{expiry}|{option_type}"
    with _MARGIN_LOCK:
        _MARGIN_CACHE[key] = {
            "data": data,
            "margin": margin,
            "cached_at": datetime.now().isoformat(),
        }


def margin_cache_clear() -> int:
    global _MARGIN_CACHE
    with _MARGIN_LOCK:
        n = len(_MARGIN_CACHE)
        _MARGIN_CACHE = {}
    return n


def start_margin_cache_autosave(interval_secs: int = 300) -> None:
    """Background thread that saves margin cache periodically."""
    def _saver():
        while True:
            time.sleep(interval_secs)
            margin_cache_save()
    t = threading.Thread(target=_saver, daemon=True)
    t.start()
