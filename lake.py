"""
lake.py -- Parquet storage for the three large append-only tables, behind
DuckDB views so every existing reader keeps working unchanged. (2026-10-05)

    ocdata         oc.duckdb        live option-chain snapshots   28.3M rows
    options_eod    bhavcopy.duckdb  F&O bhavcopy, options         72.6M rows
    span_contract  bhavcopy.duckdb  SPAN contract parameters      92.7M rows

Everything else (exposure_*, vrp, rv, cash/futures/index EOD, logs, ...) stays
in DuckDB: small, frequently rewritten by backfills, cheap to keep mutable.

Why
---
* Size: zstd Parquet measured 124 B/row for ocdata (DuckDB ~380), 20 B/row for
  options_eod and 16 for span_contract (DuckDB ~82). ~25 GB -> ~8 GB today and
  ~30 -> ~10 GB/year of growth.
* Locking: the live writer no longer opens oc.duckdb at all; it drops small
  immutable files. Dashboard, MCP and the EOD pipeline never wait on it.
* Read speed holds: DuckDB skips files from footer statistics. The EOD query
  shape `CAST(timestamp AS DATE) = d AND symbol = s` ran 14 ms over 120 day
  files vs 11 ms on a DuckDB table (benchmark, 2026-10-05).

Layout (root from storage.json, default <od>/lake)
-------------------------------------------------
    ocdata/date=YYYY-MM-DD/<HHMMSS>-<id>.parquet   live batches (append)
    ocdata/date=YYYY-MM-DD/day.parquet             after nightly compaction
    options_eod/month=YYYY-MM/month.parquet        one file per month, rewritten
    span_contract/month=YYYY-MM/month.parquet      atomically on every change
    <table>/_anchor/anchor.parquet                 0-row file carrying the schema
    _schema/<table>.json                           canonical column order + types
Folder names are for humans; views read with hive_partitioning=false, so no
extra column appears and SELECT * is identical to the legacy table.

Safety rules (each one is there because of a measured failure mode)
-------------------------------------------------------------------
1. Every write CASTs to the canonical schema and fails loudly on mismatch.
   union_by_name silently turns a column into VARCHAR for the whole view if one
   file carries text where numbers belong (verified 2026-10-05).
2. Every file change is write-temp-then-rename (atomic on one volume).
3. Compaction never leaves duplicates: batches are renamed *.old before the
   compacted file goes live, and recover() finishes any interrupted swap.
4. Mode is per table in storage.json. Until a table is cut over, writers keep
   their legacy DuckDB path; rollback = merge lake rows back + flip the mode.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import duckdb
import pandas as pd

HOME = Path(__file__).resolve().parents[1]          # <od> (oc/ is one level down)
CONFIG_FILE = HOME / "storage.json"

SPECS: Dict[str, dict] = {
    "ocdata": dict(
        db="oc.duckdb", time_col="timestamp", part="day",
        sort=["symbol", "expiry", "strike_price", "timestamp"], key=["Id"]),
    "options_eod": dict(
        db="bhavcopy.duckdb", time_col="trade_date", part="month",
        sort=["trade_date", "symbol", "expiry", "strike", "opt_type"],
        key=["trade_date", "symbol", "expiry", "strike", "opt_type"]),
    "span_contract": dict(
        db="bhavcopy.duckdb", time_col="trade_date", part="month",
        sort=["trade_date", "tag", "symbol", "instr", "expiry", "strike"],
        key=["trade_date", "tag", "symbol", "instr", "expiry", "strike"]),
}
COMPACT_QUIET_MIN = 10        # do not compact a day touched in the last N minutes
WRITER_DONE = "15:50"         # live capture is finished for the day after this


# ── config ─────────────────────────────────────────────────────────────────

def config() -> dict:
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text())
    else:
        cfg = {}
    cfg.setdefault("root", str(HOME / "lake"))
    cfg.setdefault("tables", {})
    return cfg


def save_config(cfg: dict) -> None:
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, default=str))
    _replace(tmp, CONFIG_FILE)


def root() -> Path:
    return Path(config()["root"])


def enabled(table: str) -> bool:
    """True once `table` is cut over: writers must write the lake, not DuckDB."""
    return config()["tables"].get(table, {}).get("mode") == "lake"


def tdir(table: str) -> Path:
    return root() / table


# ── schema ─────────────────────────────────────────────────────────────────

def schema_path(table: str) -> Path:
    return root() / "_schema" / f"{table}.json"


def load_schema(table: str) -> List[List[str]]:
    p = schema_path(table)
    if not p.exists():
        raise RuntimeError(f"lake: no schema for {table} at {p} (run migrate_lake.py export)")
    return json.loads(p.read_text())


def save_schema_from_db(con, table: str, source: Optional[str] = None) -> List[List[str]]:
    """Capture the legacy table's column order and exact types as canonical."""
    cols = [[r[0], r[1]] for r in con.execute(f'DESCRIBE "{source or table}"').fetchall()]
    p = schema_path(table)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cols, indent=1))
    write_anchor(table, cols)
    return cols


def write_anchor(table: str, cols: List[List[str]]) -> None:
    """0-row file with the full typed schema: the view never errors on an empty
    lake, and union_by_name always sees the canonical types."""
    d = tdir(table) / "_anchor"
    d.mkdir(parents=True, exist_ok=True)
    sel = ", ".join(f'CAST(NULL AS {t}) AS "{c}"' for c, t in cols)
    c = duckdb.connect()
    try:
        _copy(c, f"SELECT {sel} LIMIT 0", d / "anchor.parquet")
    finally:
        c.close()


def _cast_select(cols: List[List[str]], have: Iterable[str], src: str = "_in") -> str:
    have = set(have)
    parts = []
    for c, t in cols:
        parts.append(f'CAST("{c}" AS {t}) AS "{c}"' if c in have
                     else f'CAST(NULL AS {t}) AS "{c}"')
    return f"SELECT {', '.join(parts)} FROM {src}"


def _conform(table: str, cols: List[List[str]], df: pd.DataFrame) -> pd.DataFrame:
    """Rename df columns to the canonical spelling, matching case-insensitively
    (DuckDB identifiers are case-insensitive, so the legacy INSERT ... BY NAME
    accepted 'RiskReversal' for 'riskreversal'). Then reject unknown columns.
    Returns a renamed copy; the caller's frame is not modified."""
    canon = {n.lower(): n for n, _ in cols}
    seen: Dict[str, str] = {}
    for c in df.columns:
        k = str(c).lower()
        if k in seen:
            raise ValueError(f"lake[{table}]: columns {seen[k]!r} and {c!r} differ only by case")
        seen[k] = c
    ren = {c: canon[str(c).lower()] for c in df.columns
           if str(c).lower() in canon and c != canon[str(c).lower()]}
    out = df.rename(columns=ren) if ren else df
    _check_columns(table, cols, out.columns)
    return out


def _check_columns(table: str, cols: List[List[str]], df_cols: Iterable[str]) -> None:
    extra = [c for c in df_cols if c not in {n for n, _ in cols}]
    if extra:
        raise ValueError(f"lake[{table}]: columns not in canonical schema: {extra}. "
                         f"Add them deliberately (migrate_lake.py add-column) -- never "
                         f"let a writer change the schema implicitly.")


# ── file primitives ────────────────────────────────────────────────────────

def _retry(fn, *args, tries: int = 40, wait: float = 0.25):
    """Windows refuses to rename or delete a file another process has open (a
    dashboard query mid-read). Those handles are brief: retry for ~10 s."""
    for i in range(tries):
        try:
            return fn(*args)
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(wait)


def _replace(src: Path, dst: Path) -> None:
    _retry(os.replace, src, dst)


def _unlink(p: Path) -> None:
    _retry(p.unlink)


def _copy(con, select_sql: str, dest: Path) -> None:
    """COPY a SELECT to dest atomically (temp name, then rename)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex[:8]}.tmp")
    con.execute(f"COPY ({select_sql}) TO '{tmp.as_posix()}' "
                f"(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 122880)")
    _replace(tmp, dest)


def _files(d: Path) -> List[Path]:
    return sorted(p for p in d.glob("*.parquet")) if d.exists() else []


def glob(table: str) -> str:
    return (tdir(table) / "*" / "*.parquet").as_posix()


def part_dir(table: str, value) -> Path:
    v = pd.Timestamp(value)
    if SPECS[table]["part"] == "day":
        return tdir(table) / f"date={v:%Y-%m-%d}"
    return tdir(table) / f"month={v:%Y-%m}"


def _order(table: str) -> str:
    return ", ".join(f'"{c}"' for c in SPECS[table]["sort"])


# ── writes ─────────────────────────────────────────────────────────────────

def append(table: str, df: pd.DataFrame) -> int:
    """Live append (ocdata): one new immutable file per day touched. Never
    rewrites existing data, so readers are never blocked or inconsistent."""
    if df is None or df.empty:
        return 0
    cols = load_schema(table)
    df = _conform(table, cols, df)
    tc = SPECS[table]["time_col"]
    days = pd.to_datetime(df[tc]).dt.normalize()
    if days.isna().any():
        raise ValueError(f"lake[{table}]: {int(days.isna().sum())} rows with NULL {tc} -- refusing")
    df = _drop_existing_keys(table, df, days)
    if df.empty:
        return 0
    days = pd.to_datetime(df[tc]).dt.normalize()
    con = duckdb.connect()
    n = 0
    try:
        for day, grp in df.groupby(days):
            con.register("_in", grp)
            name = f"{datetime.now():%H%M%S}-{uuid.uuid4().hex[:8]}.parquet"
            _copy(con, _cast_select(cols, grp.columns) + f" ORDER BY {_order(table)}",
                  part_dir(table, day) / name)
            con.unregister("_in")
            n += len(grp)
    finally:
        con.close()
    return n


def _drop_existing_keys(table: str, df: pd.DataFrame, days: pd.Series) -> pd.DataFrame:
    """The legacy table's PRIMARY KEY rejected a second copy of a row; Parquet
    files enforce nothing. Drop rows whose key repeats within the batch or is
    already stored for the days touched, so a re-pull or retry (2026-10-12: the
    15:40 TIINDIA snapshot written at 22:15 and again at 22:21) cannot
    duplicate rows. Reads only the key column of that day's files."""
    key = SPECS[table]["key"]
    n0 = len(df)
    df = df.drop_duplicates(subset=key, keep="first")
    files = [f for d in sorted(days.unique()) for f in _files(part_dir(table, d))]
    if files:
        lst = ", ".join(f"'{f.as_posix()}'" for f in files)
        kc = ", ".join(f'"{k}"' for k in key)
        c = duckdb.connect()
        try:
            seen = c.execute(f"SELECT DISTINCT {kc} FROM read_parquet([{lst}], "
                             f"union_by_name=true)").df()
        finally:
            c.close()
        if not seen.empty:
            if len(key) == 1:
                k = key[0]
                mask = ~df[k].astype(str).isin(set(seen[k].astype(str)))
            else:
                mask = ~pd.MultiIndex.from_frame(df[key].astype(str)).isin(
                    pd.MultiIndex.from_frame(seen[key].astype(str)))
            df = df[mask]
    if len(df) < n0:
        print(f"  lake[{table}]: skipped {n0 - len(df)} row(s) whose {'/'.join(key)} "
              f"is already stored or repeated in the batch")
    return df


def _rewrite_month(con, table: str, month_dir: Path, cols, keep_sql: str,
                   add_df: Optional[pd.DataFrame]) -> int:
    """Rewrite one month partition as a single file: existing rows passing
    `keep_sql`, plus add_df (already cast). Returns resulting row count."""
    files = _files(month_dir)
    parts = []
    if files:
        lst = ", ".join(f"'{f.as_posix()}'" for f in files)
        parts.append(f"SELECT * FROM read_parquet([{lst}], union_by_name=true) WHERE {keep_sql}")
    if add_df is not None and not add_df.empty:
        con.register("_add", add_df)
        parts.append(_cast_select(cols, add_df.columns, "_add"))
    if not parts:
        return 0
    body = " UNION ALL BY NAME ".join(f"({p})" for p in parts)
    sel = ", ".join(f'"{c}"' for c, _ in cols)
    target = month_dir / "month.parquet"
    _copy(con, f"SELECT {sel} FROM ({body}) ORDER BY {_order(table)}", target)
    for f in files:                       # older day files folded into month.parquet
        if f != target:
            _unlink(f)
    return con.execute(f"SELECT count(*) FROM read_parquet('{target.as_posix()}')").fetchone()[0]


def _match_sql(match: Dict) -> str:
    conds = []
    for k, v in match.items():
        if isinstance(v, (list, tuple, set)):
            vals = ", ".join(_lit(x) for x in v)
            conds.append(f'"{k}" IN ({vals})')
        else:
            conds.append(f'"{k}" = {_lit(v)}')
    return " AND ".join(conds) if conds else "FALSE"


def _lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (date, datetime, pd.Timestamp)):
        return f"DATE '{pd.Timestamp(v):%Y-%m-%d}'"
    return "'" + str(v).replace("'", "''") + "'"


def replace_rows(table: str, df: Optional[pd.DataFrame], match: Dict) -> int:
    """DELETE rows matching `match` (equality / IN on columns, must include the
    time column) and INSERT df -- the semantics of the EOD writers' delete-then-
    insert. Month partition rewritten atomically. Returns rows written."""
    tc = SPECS[table]["time_col"]
    if tc not in match:
        raise ValueError(f"lake[{table}].replace_rows: match must include {tc}")
    cols = load_schema(table)
    if df is not None and not df.empty:
        df = _conform(table, cols, df)
    con = duckdb.connect()
    try:
        tvals = match[tc] if isinstance(match[tc], (list, tuple, set)) else [match[tc]]
        months = {part_dir(table, v) for v in tvals}
        if df is not None and not df.empty:
            months |= {part_dir(table, v) for v in pd.to_datetime(df[tc]).unique()}
        for m in sorted(months):
            sub = None
            if df is not None and not df.empty:
                sub = df[pd.to_datetime(df[tc]).map(lambda x: part_dir(table, x)) == m]
            _rewrite_month(con, table, m, cols, f"NOT ({_match_sql(match)})", sub)
    finally:
        con.close()
    return 0 if df is None else len(df)


def merge_rows(table: str, df: pd.DataFrame) -> int:
    """INSERT OR IGNORE: add rows whose key is not already present (bhavcopy_eod
    semantics). Returns rows actually added."""
    if df is None or df.empty:
        return 0
    cols = load_schema(table)
    df = _conform(table, cols, df)
    key = SPECS[table]["key"]
    tc = SPECS[table]["time_col"]
    con = duckdb.connect()
    added = 0
    try:
        for m in sorted({part_dir(table, v) for v in pd.to_datetime(df[tc]).unique()}):
            sub = df[pd.to_datetime(df[tc]).map(lambda x: part_dir(table, x)) == m]
            files = _files(m)
            if files:
                lst = ", ".join(f"'{f.as_posix()}'" for f in files)
                con.register("_new", sub)
                kc = ", ".join(f'"{k}"' for k in key)
                fresh = con.execute(
                    f"SELECT * FROM ({_cast_select(cols, sub.columns, '_new')}) n "
                    f"ANTI JOIN (SELECT {kc} FROM read_parquet([{lst}])) o USING ({kc})").df()
                con.unregister("_new")
            else:
                fresh = sub
            if not fresh.empty:
                _rewrite_month(con, table, m, cols, "TRUE", fresh)
                added += len(fresh)
    finally:
        con.close()
    return added


def null_before(table: str, column: str, cutoff) -> int:
    """SET column = NULL for rows before cutoff (span 'ra' array retention)."""
    cols = load_schema(table)
    tc = SPECS[table]["time_col"]
    con = duckdb.connect()
    n = 0
    try:
        for m in sorted(p for p in tdir(table).glob("month=*") if p.is_dir()):
            files = _files(m)
            if not files:
                continue
            lst = ", ".join(f"'{f.as_posix()}'" for f in files)
            hit = con.execute(f"SELECT count(*) FROM read_parquet([{lst}]) "
                              f"WHERE {tc} < {_lit(cutoff)} AND \"{column}\" IS NOT NULL").fetchone()[0]
            if not hit:
                continue
            sel = ", ".join(
                (f'CASE WHEN {tc} < {_lit(cutoff)} THEN NULL ELSE "{c}" END AS "{c}"'
                 if c == column else f'"{c}"') for c, _ in cols)
            _copy(con, f"SELECT {sel} FROM read_parquet([{lst}], union_by_name=true) "
                       f"ORDER BY {_order(table)}", m / "month.parquet")
            for f in files:
                if f.name != "month.parquet":
                    _unlink(f)
            n += hit
    finally:
        con.close()
    return n


def new_ids(table: str, df: pd.DataFrame) -> pd.DataFrame:
    """Rows of df whose key is not yet in the lake (failed-CSV re-import)."""
    key = SPECS[table]["key"]
    tc = SPECS[table]["time_col"]
    con = duckdb.connect()
    try:
        files = []
        for day in pd.to_datetime(df[tc]).dt.normalize().unique():
            files += _files(part_dir(table, day))
        if not files:
            return df
        lst = ", ".join(f"'{f.as_posix()}'" for f in files)
        kc = ", ".join(f'"{k}"' for k in key)
        con.register("_n", df)
        return con.execute(f"SELECT * FROM _n ANTI JOIN (SELECT {kc} FROM read_parquet([{lst}])) "
                           f"o USING ({kc})").df()
    finally:
        con.close()


# ── in-place UPDATEs, run verbatim ─────────────────────────────────────────

@contextmanager
def staged(con, table: str, d0, d1=None):
    """Run existing UPDATE SQL unchanged against lake data.

    Inside the block a TEMP base table named `table` holds every row of the
    month partitions covering [d0, d1]. DuckDB resolves the temp schema before
    main, so unqualified `UPDATE options_eod ... FROM cash_eod ...` hits it,
    joins to the real tables still work, and the view is shadowed. On a clean
    exit each month is rewritten atomically from it; on an exception nothing is
    written. (bhavcopy_eod.backfill_spot / backfill_lot / consolidate_lots.)
    """
    cols = load_schema(table)
    sel = ", ".join(f'"{c}"' for c, _ in cols)
    d1 = d1 or d0
    months = [part_dir(table, p.to_timestamp())
              for p in pd.period_range(pd.Timestamp(d0), pd.Timestamp(d1), freq="M")]
    files = [f for m in months for f in _files(m)]
    if files:
        lst = ", ".join(f"'{f.as_posix()}'" for f in files)
        con.execute(f"CREATE OR REPLACE TEMP TABLE {table} AS SELECT {sel} "
                    f"FROM read_parquet([{lst}], union_by_name=true)")
    else:
        empty = ", ".join(f'CAST(NULL AS {t}) AS "{c}"' for c, t in cols)
        con.execute(f"CREATE OR REPLACE TEMP TABLE {table} AS SELECT {empty} LIMIT 0")
    try:
        yield
        tc = SPECS[table]["time_col"]
        for m in months:
            start = pd.Timestamp(m.name.split("=", 1)[1] + "-01")
            cond = (f"{tc} >= DATE '{start:%Y-%m-%d}' AND "
                    f"{tc} < DATE '{start:%Y-%m-%d}' + INTERVAL 1 MONTH")
            n = con.execute(f"SELECT count(*) FROM temp.main.{table} WHERE {cond}").fetchone()[0]
            old = _files(m)
            if not n and not old:
                continue
            _copy(con, f"SELECT {sel} FROM temp.main.{table} WHERE {cond} "
                       f"ORDER BY {_order(table)}", m / "month.parquet")
            for f in old:
                if f.name != "month.parquet":
                    _unlink(f)
    finally:
        con.execute(f"DROP TABLE IF EXISTS temp.main.{table}")


# ── compaction (ocdata day partitions) ─────────────────────────────────────

def recover(table: str = "ocdata") -> int:
    """Finish any compaction interrupted between its rename steps."""
    # Compaction order: (1) write day.parquet.pending, (2) rename every batch to
    # *.parquet.old, (3) publish pending as day.parquet, (4) delete *.old.
    #   crash in (1)/(2): live batches still exist -> pending is redundant; drop
    #       it and un-hide any batches already renamed (publishing it would
    #       duplicate every row still visible in a live batch);
    #   crash after (2): no live batches, pending complete -> publish it;
    #   crash after (3): only leftovers -> delete the *.old files.
    fixed = 0
    for d in (p for p in tdir(table).glob("date=*") if p.is_dir()):
        pending = d / "day.parquet.pending"
        olds = list(d.glob("*.parquet.old"))
        live_batches = [f for f in _files(d) if f.name != "day.parquet"]
        if pending.exists():
            if live_batches:
                _unlink(pending)
                for f in olds:
                    _replace(f, f.with_name(f.name[:-len(".old")]))
            else:
                if (d / "day.parquet").exists() and not any(
                        o.name == "day.parquet.old" for o in olds):
                    _replace(d / "day.parquet", d / "day.parquet.old")
                _replace(pending, d / "day.parquet")
                for f in d.glob("*.parquet.old"):
                    _unlink(f)
            fixed += 1
        elif olds:
            if (d / "day.parquet").exists() and not live_batches:
                for f in olds:
                    _unlink(f)
            else:
                for f in olds:
                    _replace(f, f.with_name(f.name[:-len(".old")]))
            fixed += 1
    return fixed


def compact_day(table: str, day) -> Optional[int]:
    """Merge a day's batch files into day.parquet. Verified (row count + key
    checksum) before anything is removed. Returns rows, or None if skipped."""
    d = part_dir(table, day)
    files = _files(d)
    if len(files) <= 1:
        return None
    if time.time() - max(f.stat().st_mtime for f in files) < COMPACT_QUIET_MIN * 60:
        return None                                       # still being written
    cols = load_schema(table)
    key = ", ".join(f'"{k}"' for k in SPECS[table]["key"])
    sel = ", ".join(f'"{c}"' for c, _ in cols)
    lst = ", ".join(f"'{f.as_posix()}'" for f in files)
    con = duckdb.connect()
    try:
        src = f"read_parquet([{lst}], union_by_name=true)"
        n_src, h_src = con.execute(f"SELECT count(*), bit_xor(hash({key})) FROM {src}").fetchone()
        n_dup = con.execute(f"SELECT count(*) - count(DISTINCT ({key})) FROM {src}").fetchone()[0]
        body = (f"SELECT {sel} FROM {src} QUALIFY row_number() OVER (PARTITION BY {key}) = 1"
                if n_dup else f"SELECT {sel} FROM {src}")
        pending = d / "day.parquet.pending"
        tmp = d / f".compact.{uuid.uuid4().hex[:8]}.tmp"
        con.execute(f"COPY ({body} ORDER BY {_order(table)}) TO '{tmp.as_posix()}' "
                    f"(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 122880)")
        n_new, h_new = con.execute(
            f"SELECT count(*), bit_xor(hash({key})) FROM read_parquet('{tmp.as_posix()}')").fetchone()
        if n_new != n_src - n_dup or (not n_dup and h_new != h_src):
            _unlink(tmp)
            raise RuntimeError(f"lake[{table}] compaction of {d.name} failed verification "
                               f"({n_src} rows, {n_dup} dup -> {n_new}); batches left untouched")
        _replace(tmp, pending)
    finally:
        con.close()
    for f in files:                                       # hide batches ...
        _replace(f, f.with_name(f.name + ".old"))
    _replace(pending, d / "day.parquet")                  # ... then publish
    for f in d.glob("*.parquet.old"):
        _unlink(f)
    return n_src - n_dup


def maintain(now: Optional[datetime] = None) -> Dict[str, int]:
    """Nightly: recover, then compact every finished ocdata day."""
    now = now or datetime.now()
    out = {"recovered": recover("ocdata"), "compacted_days": 0}
    for d in sorted(p for p in tdir("ocdata").glob("date=*") if p.is_dir()):
        day = date.fromisoformat(d.name.split("=", 1)[1])
        if day == now.date() and now.strftime("%H:%M") < WRITER_DONE:
            continue
        if compact_day("ocdata", day) is not None:
            out["compacted_days"] += 1
    return out


# ── views ──────────────────────────────────────────────────────────────────

def view_sql(table: str) -> str:
    cols = load_schema(table)
    sel = ", ".join(f'"{c}"' for c, _ in cols)
    return (f"CREATE OR REPLACE VIEW {table} AS SELECT {sel} FROM read_parquet('{glob(table)}', "
            f"hive_partitioning=false, union_by_name=true)")


def stats(table: str) -> Dict[str, float]:
    files = [f for f in tdir(table).rglob("*.parquet")]
    return {"files": len(files), "gb": round(sum(f.stat().st_size for f in files) / 1e9, 3)}


if __name__ == "__main__":                                # nightly entry point
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "maintain":
        if not enabled("ocdata"):
            print("lake maintain: ocdata not cut over -- nothing to do")
        else:
            print("lake maintain:", maintain())
    else:
        print(__doc__)
