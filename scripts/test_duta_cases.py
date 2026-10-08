"""Offline test of fetch_duta_cases' counting rules. No credential needed.

    python scripts/test_duta_cases.py

Covers: a confirmed line counting once; a quotation staying out of the sold
total but into `quoted`; a credit note subtracting; a day being the Maldives
(UTC+5) day, so an order placed at 22:00 UTC lands on the NEXT calendar day;
the profit % formula (negative allowed, unknown cost -> None), and the product-name cleanup (code prefix, bilingual colour, pack and size).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_duta_cases as f

assert f.maldives_day("2026-09-29 22:00:00") == "2026-09-30", "UTC+5 rollover"
assert f.maldives_day("2026-09-29 15:49:10") == "2026-09-29"

assert f.clean_product("[1069260] Soklin Floor Cleaner Apple (Green / Hijau Muda) - Case (12pcs, 400 ml)") == (
    "Soklin Floor Cleaner Apple (Green)", "400 ml", "12 pcs")
assert f.clean_product("Better Fun Bites - Case (6 bundles, 100 gr)") == ("Better Fun Bites", "100 gr", "6 bundles")
assert f.clean_product("Soklin Detergent Liquid White - Case (6pcs, 1 liter)") == ("Soklin Detergent Liquid White", "1 liter", "6 pcs")

sale = [
    {"product_id": 1, "qty": 5.0, "order": "S1", "date": "2026-09-30"},
    {"product_id": 2, "qty": 2.0, "order": "S1", "date": "2026-09-30"},
    {"product_id": 1, "qty": 3.0, "order": "S2", "date": "2026-10-01"},
]
quoted = [{"product_id": 1, "qty": 9.0, "order": "S3"}, {"product_id": 2, "qty": 1.0, "order": "S3"}]
pos = [{"product_id": 2, "qty": 1.0, "order": "Ike - 1", "date": "2026-10-01"}]
refund = [{"product_id": 1, "qty": 1.0, "order": "RINV/1", "date": "2026-10-01"}]

days, q, totals = f.aggregate([], sale, quoted, pos, refund)
assert q == {"cases": 10.0, "orders": 1}, q
assert totals == {1: 7.0, 2: 3.0}, totals
assert [(d["date"], d["cases"], d["orders"]) for d in days] == [("2026-09-30", 7.0, 1), ("2026-10-01", 3.0, 2)], days
assert days[1]["byProduct"] == {"1": 2.0, "2": 1.0}, days[1]
assert sum(d["cases"] for d in days) == sum(totals.values()) == 10.0

assert f.margin_pct(273.49, 304.63) == 10.2, f.margin_pct(273.49, 304.63)
assert f.margin_pct(100, 80) == -25.0, "selling below cost is a negative margin, not hidden"
assert f.margin_pct(0, 300) is None and f.margin_pct(100, 0) is None and f.margin_pct(None, 5) is None

print("ok")
