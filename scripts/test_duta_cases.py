"""Offline test of fetch_duta_cases' counting rules. No credential needed.

    python scripts/test_duta_cases.py

Covers: a confirmed line counting once; a quotation staying out of the sold
total but into `quoted`; a credit note subtracting; a day being the Maldives
(UTC+5) day, so an order placed at 22:00 UTC lands on the NEXT calendar day;
the profit % formula (floored at 0, unknown cost -> None), the money profit per case (NOT floored, unknown -> None), the per-shipment profit lines and the proforma landed-cost conversion, and the product-name cleanup (code prefix, bilingual colour, pack and size).
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
assert f.margin_pct(100, 80) == 0.0, "below cost reads 0%, never negative"
assert f.margin_pct(100, 100) == 0.0
assert f.margin_pct(0, 300) is None and f.margin_pct(100, 0) is None and f.margin_pct(None, 5) is None

assert f.unit_profit(273.49, 304.63) == 31.14, f.unit_profit(273.49, 304.63)
assert f.unit_profit(313.52, 244.44) == -69.08, "below cost stays negative so the shipment total is honest"
assert f.unit_profit(100, 100) == 0.0
assert f.unit_profit(0, 300) is None and f.unit_profit(100, 0) is None and f.unit_profit(None, 5) is None

lines = f.shipment_lines([(2, 10, 100.0), (1, 4, 313.52), (3, 5, 50.0)], {1: 244.44, 2: 130.0, 3: None})
assert [l["productId"] for l in lines] == [1, 2, 3], "sorted by product id"
assert lines[0]["profitPerCase"] == -69.08 and lines[0]["profit"] == -276.32, lines[0]
assert lines[1] == {"productId": 2, "cases": 10, "cost": 100.0, "netPrice": 130.0, "profitPerCase": 30.0, "profit": 300.0}, lines[1]
assert lines[2]["profit"] is None and lines[2]["netPrice"] is None, "no price -> no profit, not a guess"

# No usdPrice -> Odoo's cost as is; with usdPrice -> 9.09 USD x 21.61 x 1.35 = 265.19
rows = f.proforma_rows({"mvrPerUsd": 21.61, "landedFactor": 1.35,
                        "lines": [{"productId": 2034, "cases": 140},
                                  {"productId": 2030, "cases": 70, "usdPrice": 9.09}]},
                       {2034: 131.74, 2030: 256.82})
assert rows == [(2034, 140, 131.74), (2030, 70, 265.19)], rows

print("ok")
