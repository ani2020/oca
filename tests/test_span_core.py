"""Property tests for span_core.

Run standalone:  python tests/test_span_core.py
Run under pytest: pytest tests/test_span_core.py

Uses a synthetic SPAN 4.00 file with the same element layout as NSE's
nsccl.*.zip (phyPf / futPf / oopPf / series / opt / ra). The RELIANCE option
risk array is copied verbatim from nsccl.20261001.s.spn so the sign
convention is pinned to real data.

Optional golden check: set SPAN_GOLDEN to a real nsccl.20261001.i4.zip and
the RELIANCE Nov-26 1060 PE short scan must read 75.92 per unit.

  1  parse: meta, underlyings, futures, options, test portfolios skipped
  2  sign convention: short scan = max(-a), long scan = max(a)
  3  single short leg margin = scan * qty + ELM on underlying value
  4  long option: no exposure, no premium credit
  5  netting: short strangle < sum of the two legs
  6  netting: long future + short call (covered call) < naked call + future
  7  broker_mult scales SPAN only
  8  missing legs are reported, not silently zero
  9  symbol filter and zip input
"""
import io
import os
import sys
import zipfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import span_core as sp  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        fails.append(name)


def ra(vals):
    return "<ra><r>1</r>" + "".join(f"<a>{v:.2f}</a>" for v in vals) + "<d>1.00000</d></ra>"


# Real RELIANCE 2026-11-23 1060 PE risk array (EOD 2026-10-01)
REL_PUT = [-10.26, 5.85, -1.92, 5.85, -24.05, 2.16, 2.78, 5.85,
           -45.11, -13.79, 5.24, 5.85, -74.73, -48.38, 2.62, -72.68]
# NSE scenario order: 1-2 unchanged (vol up/down), 3-4 up 1/3, 5-6 down 1/3,
# 7-8 up 2/3, 9-10 down 2/3, 11-12 up 3/3, 13-14 down 3/3, 15 extreme up,
# 16 extreme down. The real put above is worst at 13 and 16, as it must be.
# Synthetic OTM call: long gains (a < 0) on up moves, loses ~premium on down.
REL_CALL = [-2.0, 3.0, -15.0, -12.0, 6.0, 6.0, -38.0, -33.0,
            6.0, 6.0, -72.0, -66.0, 6.0, 6.0, -68.0, 6.0]
# Future: linear in the scan moves (+/- 1/3, 2/3, 3/3 of 165.74), long loses on down
psr = 165.74
FUT = [0, 0, -psr/3, -psr/3, psr/3, psr/3, -2*psr/3, -2*psr/3, 2*psr/3, 2*psr/3,
       -psr, -psr, psr, psr, -0.35*2*psr, 0.35*2*psr]

XML = f"""<?xml version="1.0"?>
<spanFile><fileFormat>4.00</fileFormat><created>202610011621</created>
<definitions><currencyDef><currency>INR</currency></currencyDef></definitions>
<pointInTime><date>20261001</date><isSetl>1</isSetl>
<clearingOrg><ec>NSCCL</ec><exchange><exch>NSE</exch>
<phyPf><pfId>1</pfId><pfCode>011NSETEST</pfCode><name>X</name>
 <phy><cId>1</cId><pe>00000000</pe><p>200.00</p><d>0</d><v>0</v>
 <scanRate><r>1</r><priceScan>49.19</priceScan><volScan>0.10</volScan></scanRate>{ra([0]*16)}</phy></phyPf>
<phyPf><pfId>589</pfId><pfCode>RELIANCE</pfCode><name>RELIANCE</name>
 <phy><cId>1</cId><pe>00000000</pe><p>1167.210000</p><d>0</d><v>0</v>
 <scanRate><r>1</r><priceScan>165.74</priceScan><volScan>0.10</volScan></scanRate>{ra([0]*16)}</phy></phyPf>
<phyPf><pfId>590</pfId><pfCode>HDFCBANK</pfCode><name>HDFCBANK</name>
 <phy><cId>1</cId><pe>00000000</pe><p>720.40</p><d>0</d><v>0</v>
 <scanRate><r>1</r><priceScan>102.30</priceScan><volScan>0.10</volScan></scanRate>{ra([0]*16)}</phy></phyPf>
<futPf><pfId>600</pfId><pfCode>RELIANCE</pfCode><name>RELIANCE</name>
 <fut><cId>2</cId><pe>20261123</pe><p>1174.50</p><d>1.0</d><v>0</v>
 <undC><exch>NSE</exch><pfId>589</pfId><cId>1</cId></undC>
 <scanRate><r>1</r><priceScan>165.74</priceScan><volScan>0</volScan></scanRate>{ra(FUT)}</fut></futPf>
<oopPf><pfId>591</pfId><pfCode>RELIANCE</pfCode><name>RELIANCE</name><exercise>EURO</exercise>
 <undPf><exch>NSE</exch><pfId>589</pfId><pfCode>RELIANCE</pfCode><pfType>PHY</pfType></undPf>
 <series><pe>20261123</pe><v>0.254</v><setlDate>20261123</setlDate><t>0.14</t>
  <undC><exch>NSE</exch><pfId>589</pfId><cId>1</cId></undC>
  <scanRate><r>1</r><priceScan>165.74</priceScan><volScan>0.10</volScan></scanRate>
  <opt><cId>1</cId><o>P</o><k>1060.00</k><p>5.85</p><d>-0.13079612</d><v>0.2540</v>{ra(REL_PUT)}</opt>
  <opt><cId>2</cId><o>C</o><k>1280.00</k><p>6.00</p><d>0.12</d><v>0.2460</v>{ra(REL_CALL)}</opt>
 </series>
</oopPf>
</exchange></clearingOrg></pointInTime></spanFile>
"""
SPN = XML.encode()


def zipped(b: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("nsccl.20261001.s.spn", b)
    return buf.getvalue()


EXP = date(2026, 11, 23)

print("\n=== 1. parse ===")
sf = sp.parse_span(SPN)
check("trade_date from pointInTime", sf.trade_date == date(2026, 10, 1), str(sf.trade_date))
check("created timestamp", sf.created is not None and sf.created.hour == 16, str(sf.created))
check("test portfolio skipped", "011NSETEST" not in sf.underlyings)
check("two real underlyings", set(sf.underlyings) == {"RELIANCE", "HDFCBANK"}, str(set(sf.underlyings)))
u = sf.underlyings["RELIANCE"]
check("PSR 14.2% of price", abs(u.psr_pct - 0.142) < 0.001, f"{u.psr_pct:.4f}")
check("three contracts (1 fut, 2 opt)", len(sf.contracts) == 3, str(len(sf.contracts)))
put = sf.contracts[sp.contract_key("RELIANCE", "P", EXP, 1060)]
check("option expiry from series pe", put.expiry == EXP)
check("undPf pfCode did not leak into option symbol", put.symbol == "RELIANCE")
fut = sf.contracts[sp.contract_key("RELIANCE", "FUT", EXP, 0)]
check("future parsed with strike 0", fut.strike == 0.0 and fut.price == 1174.5)

print("\n=== 2. sign convention ===")
check("short scan of real put = 74.73", abs(put.short_scan - 74.73) < 1e-9, f"{put.short_scan}")
check("long scan = premium-ish max(a)", abs(put.long_scan - 5.85) < 1e-9, f"{put.long_scan}")

book = sp.SpanBook(sf.underlyings, sf.contracts, sf.trade_date, "s")

print("\n=== 3. single short leg ===")
r = sp.short_option_margin(book, "RELIANCE", "PE", EXP, 1060, lot=500)
check("SPAN = 74.73 x 500", abs(r.span - 37365.0) < 0.01, f"{r.span}")
check("ELM = 3.5% x 1167.21 x 500", abs(r.exposure - 0.035 * 1167.21 * 500) < 0.01, f"{r.exposure}")
check("total = span + exposure", abs(r.total - (r.span + r.exposure)) < 0.01)
check("premium credit = 5.85 x 500", abs(r.premium_received - 2925.0) < 0.01)
check("margin ~ 10-11% of notional (matches Zerodha order)",
      0.09 < r.total / (1167.21 * 500) < 0.12, f"{r.total/(1167.21*500):.4f}")

print("\n=== 4. long option ===")
rl = sp.portfolio_margin([sp.Leg("RELIANCE", "PE", EXP, 1060, 500)], book)
check("long put: no exposure", rl.exposure == 0.0)
check("long put: no premium credit", rl.premium_received == 0.0)
check("long put span = max(a) x qty", abs(rl.span - 5.85 * 500) < 0.01, f"{rl.span}")

print("\n=== 5. netting: short strangle ===")
legs = [sp.Leg("RELIANCE", "PE", EXP, 1060, -500), sp.Leg("RELIANCE", "CE", EXP, 1280, -500)]
rs = sp.portfolio_margin(legs, book)
p_only = sp.portfolio_margin(legs[:1], book).span
c_only = sp.portfolio_margin(legs[1:], book).span
check("strangle SPAN < sum of legs", rs.span < p_only + c_only, f"{rs.span} vs {p_only + c_only}")
# Not ">= larger leg": in the put's worst scenario the short call decays and
# its gain offsets (exchange arrays include option value). Bound is the larger
# leg minus the other leg's maximum possible gain (its long_scan x qty).
call_c = book.contract(sp.contract_key("RELIANCE", "CE", EXP, 1280))
check("strangle SPAN >= larger leg - other leg's max gain",
      rs.span >= max(p_only, c_only) - call_c.long_scan * 500 - 1e-6,
      f"{rs.span} vs {max(p_only, c_only) - call_c.long_scan * 500}")
check("ELM charged on both short legs", abs(rs.exposure - 2 * 0.035 * 1167.21 * 500) < 0.01)

print("\n=== 6. netting: covered call (long fut + short call) ===")
cc = sp.portfolio_margin([sp.Leg("RELIANCE", "FUT", EXP, 0, 500),
                          sp.Leg("RELIANCE", "CE", EXP, 1280, -500)], book)
naked = sp.portfolio_margin([sp.Leg("RELIANCE", "CE", EXP, 1280, -500)], book).span
fut_only = sp.portfolio_margin([sp.Leg("RELIANCE", "FUT", EXP, 0, 500)], book).span
check("covered SPAN < naked call + future", cc.span < naked + fut_only, f"{cc.span} vs {naked + fut_only}")

print("\n=== 7. broker_mult ===")
r2 = sp.short_option_margin(book, "RELIANCE", "PE", EXP, 1060, lot=500, broker_mult=1.12)
check("span unchanged, total scaled on span only",
      abs(r2.total - (1.12 * r2.span + r2.exposure)) < 0.01, f"{r2.total}")

print("\n=== 8. missing legs ===")
rm = sp.portfolio_margin([sp.Leg("RELIANCE", "PE", EXP, 999, -500)], book)
check("missing leg reported", len(rm.missing) == 1 and rm.span == 0.0)
check("short_option_margin returns None on miss",
      sp.short_option_margin(book, "RELIANCE", "PE", EXP, 999, lot=500) is None)

print("\n=== 9. filter + zip input ===")
sz = sp.parse_span(zipped(SPN), symbols={"HDFCBANK"})
check("zip input parses", sz.trade_date == date(2026, 10, 1))
check("symbol filter keeps only HDFCBANK", set(sz.underlyings) == {"HDFCBANK"} and not sz.contracts)

golden = os.environ.get("SPAN_GOLDEN")
if golden and Path(golden).exists():
    print("\n=== golden: real nsccl.20261001.i4.zip ===")
    gb = sp.SpanBook.from_file(golden, symbols={"RELIANCE"})
    gc = gb.contract(sp.contract_key("RELIANCE", "PE", EXP, 1060))
    check("RELIANCE Nov 1060 PE short scan 75.92", gc is not None and abs(gc.short_scan - 75.92) < 1e-6,
          f"{gc.short_scan if gc else None}")

print("\n" + "=" * 62)
print(f"  {'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + ', '.join(fails)}")
print("=" * 62)


def test_all_properties():
    """pytest entry point."""
    assert not fails, f"failed: {fails}"
