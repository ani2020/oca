# Tests

Fast regression safety net for the OC dashboard.

## Run
```
pip install pytest duckdb fastapi httpx uvicorn pandas numpy scipy --break-system-packages
python -m pytest                 # all tests
python -m pytest tests/test_exposure_core.py    # pure-math only (fast)
```

## What's covered
- **test_exposure_core.py** — pure exposure math (gamma flip, range, transition
  width, lopsidedness, signal derivation, confidence). Deterministic, no DB.
- **test_endpoints_smoke.py** — every API endpoint returns 200/valid-JSON against
  a synthetic in-memory DuckDB fixture. Catches import errors, NaN serialisation,
  and SQL bugs (the regression class seen during the refactor).

## When to run
Before committing any change to routes, helpers, exposure_core, or the batch
script. The pure-math suite runs in <5s.

## Fixture note
The synthetic `ocdata` fixture in test_endpoints_smoke.py mirrors the production
column set. If you add columns to oc_processor / the DB schema that an endpoint
SELECTs, add them to the fixture's CREATE TABLE + row tuple too.
