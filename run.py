#!/usr/bin/env python3
"""
run.py  —  NSE OC Analytics Dashboard launcher.

Usage:
    python run.py                         # oc.duckdb in CWD, port 8000
    python run.py --db /path/to/oc.duckdb
    python run.py --port 8080
    python run.py --host 0.0.0.0          # expose to LAN
"""
import sys
from pathlib import Path

# Always add the directory that contains this file to sys.path so that
# `oc_dashboard` is importable regardless of the working directory.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from oc_dashboard.app import main
main()
